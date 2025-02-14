#   Copyright 2022 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""This module specializes on running MCMCs with population step methods."""

import logging

from copy import copy
from typing import Iterator, List, Sequence, Union

import cloudpickle
import numpy as np

from fastprogress.fastprogress import progress_bar
from typing_extensions import TypeAlias

from pymc.backends import _init_trace
from pymc.backends.base import BaseTrace, MultiTrace
from pymc.initial_point import PointType
from pymc.model import modelcontext
from pymc.stats.convergence import log_warning_stats
from pymc.step_methods import CompoundStep
from pymc.step_methods.arraystep import BlockedStep, PopulationArrayStepShared
from pymc.util import RandomSeed

__all__ = ()


Step: TypeAlias = Union[BlockedStep, CompoundStep]


_log = logging.getLogger("pymc")


def _sample_population(
    draws: int,
    chains: int,
    start: Sequence[PointType],
    random_seed: RandomSeed,
    step,
    tune: int,
    model,
    progressbar: bool = True,
    parallelize: bool = False,
    **kwargs,
) -> MultiTrace:
    """Performs sampling of a population of chains using the ``PopulationStepper``.

    Parameters
    ----------
    draws : int
        The number of samples to draw
    chains : int
        The total number of chains in the population
    start : list
        Start points for each chain
    random_seed : single random seed, optional
    step : function
        Step function (should be or contain a population step method)
    tune : int
        Number of iterations to tune.
    model : Model (optional if in ``with`` context)
    progressbar : bool
        Show progress bars? (defaults to True)
    parallelize : bool
        Setting for multiprocess parallelization

    Returns
    -------
    trace : MultiTrace
        Contains samples of all chains
    """
    sampling = _prepare_iter_population(
        draws,
        step,
        start,
        parallelize,
        tune=tune,
        model=model,
        random_seed=random_seed,
        progressbar=progressbar,
    )

    if progressbar:
        sampling = progress_bar(sampling, total=draws, display=progressbar)

    latest_traces = None
    for it, traces in enumerate(sampling):
        latest_traces = traces
    return MultiTrace(latest_traces)


class PopulationStepper:
    """Wraps population of step methods to step them in parallel with single or multiprocessing."""

    def __init__(self, steppers, parallelize: bool, progressbar: bool = True):
        """Use multiprocessing to parallelize chains.

        Falls back to sequential evaluation if multiprocessing fails.

        In the multiprocessing mode of operation, a new process is started for each
        chain/stepper and Pipes are used to communicate with the main process.

        Parameters
        ----------
        steppers : list
            A collection of independent step methods, one for each chain.
        parallelize : bool
            Indicates if parallelization via multiprocessing is desired.
        progressbar : bool
            Should we display a progress bar showing relative progress?
        """
        self.nchains = len(steppers)
        self.is_parallelized = False
        self._primary_ends = []
        self._processes = []
        self._steppers = steppers
        if parallelize:
            try:
                # configure a child process for each stepper
                _log.info(
                    "Attempting to parallelize chains to all cores. You can turn this off with `pm.sample(cores=1)`."
                )
                import multiprocessing

                for c, stepper in (
                    enumerate(progress_bar(steppers)) if progressbar else enumerate(steppers)
                ):
                    secondary_end, primary_end = multiprocessing.Pipe()
                    stepper_dumps = cloudpickle.dumps(stepper, protocol=4)
                    process = multiprocessing.Process(
                        target=self.__class__._run_secondary,
                        args=(c, stepper_dumps, secondary_end),
                        name=f"ChainWalker{c}",
                    )
                    # we want the child process to exit if the parent is terminated
                    process.daemon = True
                    # Starting the process might fail and takes time.
                    # By doing it in the constructor, the sampling progress bar
                    # will not be confused by the process start.
                    process.start()
                    self._primary_ends.append(primary_end)
                    self._processes.append(process)
                self.is_parallelized = True
            except Exception:
                _log.info(
                    "Population parallelization failed. "
                    "Falling back to sequential stepping of chains."
                )
                _log.debug("Error was: ", exc_info=True)
        else:
            _log.info(
                "Chains are not parallelized. You can enable this by passing "
                "`pm.sample(cores=n)`, where n > 1."
            )
        return super().__init__()

    def __enter__(self):
        """Do nothing: processes are already started in ``__init__``."""
        return

    def __exit__(self, exc_type, exc_val, exc_tb):
        if len(self._processes) > 0:
            try:
                for primary_end in self._primary_ends:
                    primary_end.send(None)
                for process in self._processes:
                    process.join(timeout=3)
            except Exception:
                _log.warning("Termination failed.")
        return

    @staticmethod
    def _run_secondary(c, stepper_dumps, secondary_end):
        """This method is started on a separate process to perform stepping of a chain.

        Parameters
        ----------
        c : int
            number of this chain
        stepper : BlockedStep
            a step method such as CompoundStep
        secondary_end : multiprocessing.connection.PipeConnection
            This is our connection to the main process
        """
        # re-seed each child process to make them unique
        np.random.seed(None)
        try:
            stepper = cloudpickle.loads(stepper_dumps)
            # the stepper is not necessarily a PopulationArraySharedStep itself,
            # but rather a CompoundStep. PopulationArrayStepShared.population
            # has to be updated, therefore we identify the substeppers first.
            population_steppers = []
            for sm in stepper.methods if isinstance(stepper, CompoundStep) else [stepper]:
                if isinstance(sm, PopulationArrayStepShared):
                    population_steppers.append(sm)
            while True:
                incoming = secondary_end.recv()
                # receiving a None is the signal to exit
                if incoming is None:
                    break
                tune_stop, population = incoming
                if tune_stop:
                    stepper.stop_tuning()
                # forward the population to the PopulationArrayStepShared objects
                # This is necessary because due to the process fork, the population
                # object is no longer shared between the steppers.
                for popstep in population_steppers:
                    popstep.population = population
                update = stepper.step(population[c])
                secondary_end.send(update)
        except Exception:
            _log.exception(f"ChainWalker{c}")
        return

    def step(self, tune_stop: bool, population):
        """Step the entire population of chains.

        Parameters
        ----------
        tune_stop : bool
            Indicates if the condition (i == tune) is fulfilled
        population : list
            Current Points of all chains

        Returns
        -------
        update : list
            List of (Point, stats) tuples for all chains
        """
        updates = [None] * self.nchains
        if self.is_parallelized:
            for c in range(self.nchains):
                self._primary_ends[c].send((tune_stop, population))
            # Blockingly get the step outcomes
            for c in range(self.nchains):
                updates[c] = self._primary_ends[c].recv()
        else:
            for c in range(self.nchains):
                if tune_stop:
                    self._steppers[c].stop_tuning()
                updates[c] = self._steppers[c].step(population[c])
        return updates


def _prepare_iter_population(
    draws: int,
    step,
    start: Sequence[PointType],
    parallelize: bool,
    tune: int,
    model=None,
    random_seed: RandomSeed = None,
    progressbar=True,
) -> Iterator[Sequence[BaseTrace]]:
    """Prepare a PopulationStepper and traces for population sampling.

    Parameters
    ----------
    draws : int
        The number of samples to draw
    step : function
        Step function (should be or contain a population step method)
    start : list
        Start points for each chain
    parallelize : bool
        Setting for multiprocess parallelization
    tune : int
        Number of iterations to tune.
    model : Model (optional if in ``with`` context)
    random_seed : single random seed, optional
    progressbar : bool
        ``progressbar`` argument for the ``PopulationStepper``, (defaults to True)

    Returns
    -------
    _iter_population : generator
        Yields traces of all chains at the same time
    """
    nchains = len(start)
    model = modelcontext(model)
    draws = int(draws)

    if draws < 1:
        raise ValueError("Argument `draws` should be above 0.")

    if random_seed is not None:
        np.random.seed(random_seed)

    # The initialization of traces, samplers and points must happen in the right order:
    # 1. population of points is created
    # 2. steppers are initialized and linked to the points object
    # 3. traces are initialized
    # 4. a PopulationStepper is configured for parallelized stepping

    # 1. create a population (points) that tracks each chain
    # it is updated as the chains are advanced
    population = [start[c] for c in range(nchains)]

    # 2. Set up the steppers
    steppers: List[Step] = []
    for c in range(nchains):
        # need indepenent samplers for each chain
        # it is important to copy the actual steppers (but not the delta_logp)
        if isinstance(step, CompoundStep):
            chainstep = CompoundStep([copy(m) for m in step.methods])
        else:
            chainstep = copy(step)
        # link population samplers to the shared population state
        for sm in chainstep.methods if isinstance(step, CompoundStep) else [chainstep]:
            if isinstance(sm, PopulationArrayStepShared):
                sm.link_population(population, c)
        steppers.append(chainstep)

    # 3. Initialize a BaseTrace for each chain
    traces: List[BaseTrace] = [
        _init_trace(
            expected_length=draws + tune,
            stats_dtypes=steppers[c].stats_dtypes,
            chain_number=c,
            trace=None,
            model=model,
        )
        for c in range(nchains)
    ]

    # 4. configure the PopulationStepper (expensive call)
    popstep = PopulationStepper(steppers, parallelize, progressbar=progressbar)

    # Because the preparations above are expensive, the actual iterator is
    # in another method. This way the progbar will not be disturbed.
    return _iter_population(draws, tune, popstep, steppers, traces, population)


def _iter_population(
    draws: int, tune: int, popstep: PopulationStepper, steppers, traces: Sequence[BaseTrace], points
) -> Iterator[Sequence[BaseTrace]]:
    """Iterate a ``PopulationStepper``.

    Parameters
    ----------
    draws : int
        number of draws per chain
    tune : int
        number of tuning steps
    popstep : PopulationStepper
        the helper object for (parallelized) stepping of chains
    steppers : list
        The step methods for each chain
    traces : list
        Traces for each chain
    points : list
        population of chain states

    Yields
    ------
    traces : list
        List of trace objects of the individual chains
    """
    try:
        with popstep:
            # iterate draws of all chains
            for i in range(draws):
                # this call steps all chains and returns a list of (point, stats)
                # the `popstep` may interact with subprocesses internally
                updates = popstep.step(i == tune, points)

                # apply the update to the points and record to the traces
                for c, strace in enumerate(traces):
                    if steppers[c].generates_stats:
                        points[c], stats = updates[c]
                        strace.record(points[c], stats)
                        log_warning_stats(stats)
                    else:
                        points[c] = updates[c]
                        strace.record(points[c])
                # yield the state of all chains in parallel
                yield traces
    except KeyboardInterrupt:
        for c, strace in enumerate(traces):
            strace.close()
            if hasattr(steppers[c], "report"):
                steppers[c].report._finalize(strace)
        raise
    except BaseException:
        for c, strace in enumerate(traces):
            strace.close()
        raise
    else:
        for c, strace in enumerate(traces):
            strace.close()
            if hasattr(steppers[c], "report"):
                steppers[c].report._finalize(strace)
