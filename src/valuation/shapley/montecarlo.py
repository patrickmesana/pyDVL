"""
Simple implementation of DataShapley [1].

TODO:
 * don't copy data to all workers foolishly
 * compute shapley values for groups of samples
 * use ray / whatever to distribute jobs to multiple machines
 * ...
"""

import multiprocessing as mp
import numpy as np
import queue

from time import time
from unittest.mock import Mock
from typing import Callable, Dict, List, Tuple
from collections import OrderedDict
from tqdm.auto import tqdm, trange

from valuation.reporting.scores import sort_values_history
from valuation.utils import Dataset, SupervisedModel, vanishing_derivatives


__all__ = ['parallel_montecarlo_shapley', 'serial_montecarlo_shapley']


class ShapleyWorker(mp.Process):
    """ A simple consumer worker using two queues.

     TODO: use shared memory to avoid copying data
     """

    def __init__(self,
                 worker_id: int,
                 tasks: mp.Queue,
                 results: mp.Queue,
                 converged: mp.Value,
                 model: SupervisedModel,
                 global_score: float,
                 data: Dataset,
                 min_samples: int,
                 score_tolerance: float,
                 progress_bar: bool = False):
        # Mark as daemon, so we are killed when the parent exits (e.g. Ctrl+C)
        super().__init__(daemon=True)

        self.id = worker_id
        self.tasks = tasks
        self.results = results
        self.converged = converged
        self.model = model
        self.global_score = global_score
        self.data = data
        self.min_samples = min_samples
        self.score_tolerance = score_tolerance
        self.num_samples = len(self.data)
        self.progress_bar = progress_bar

    def run(self):
        task = self.tasks.get(timeout=1.0)  # Wait a bit during start-up (yikes)
        while True:
            if task is None:  # Indicates we are done
                self.tasks.put(None)
                return

            result = self._run(task)

            # Check whether the None flag was sent during _run().
            # This throws away our last results, but avoids a deadlock (can't
            # join the process if a queue has items)
            try:
                task = self.tasks.get_nowait()
                if task is not None:
                    self.results.put(result)
            except queue.Empty:
                return

    def _run(self, permutation: np.ndarray) \
            -> Tuple[np.ndarray, np.ndarray, int]:
        """ """
        # scores[0] is the value of training on the empty set.
        n = len(permutation)
        scores = np.zeros(n + 1)
        if self.progress_bar:
            pbar = tqdm(total=self.num_samples, position=self.id,
                        desc=f"{self.name}", leave=False)
        else:
            pbar = Mock()  # HACK, sue me.
        pbar.reset()
        early_stop = None
        for j, index in enumerate(permutation, start=1):
            if self.converged.value >= self.num_samples:
                break

            mean_last_score = scores[max(j - self.min_samples, 0):j].mean()
            pbar.set_postfix_str(
                    f"last {self.min_samples} scores: {mean_last_score:.2e}")

            # Stop if last min_samples have an average mean below threshold:
            if abs(self.global_score - mean_last_score) < self.score_tolerance:
                if early_stop is None:
                    early_stop = j
                scores[j] = scores[j - 1]
            else:
                x = self.data.x_train.iloc[permutation[:j + 1]]
                y = self.data.y_train.iloc[permutation[:j + 1]]
                try:
                    self.model.fit(x, y.values.ravel())
                    scores[j] = self.model.score(self.data.x_test,
                                                 self.data.y_test.values.ravel())
                except:
                    scores[j] = np.nan
            pbar.update()
        pbar.close()
        # FIXME: sending the permutation back is wasteful
        return permutation, scores, early_stop


def parallel_montecarlo_shapley(model: SupervisedModel,
                                data: Dataset,
                                bootstrap_iterations: int,
                                min_samples: int,
                                score_tolerance: float,
                                min_values: int,
                                value_tolerance: float,
                                max_permutations: int,
                                num_workers: int,
                                run_id: int = 0,
                                worker_progress: bool = False) \
        -> Tuple[OrderedDict, List[int]]:
    """ MonteCarlo approximation to the Shapley value of data points.

    Instead of naively implementing the expectation, we sequentially add points
    to a dataset from a permutation. We compute scores and stop when model
    performance doesn't increase beyond a threshold.

    We keep sampling permutations and updating all values until the change in
    the moving average for all values falls below another threshold.

        :param model: sklearn model / pipeline
        :param data: split dataset
        :param bootstrap_iterations: Repeat global_score computation this many
         times to estimate variance.
        :param min_samples: Use so many of the last samples for a permutation
         in order to compute the moving average of scores.
        :param score_tolerance: For every permutation, computation stops
         after the mean increase in performance is within score_tolerance*stddev
         of the bootstrapped score over the **test** set (i.e. we bootstrap to
         compute variance of scores, then use that to stop)
        :param min_values: complete at least these many value computations for
         every index and use so many of the last values for each sample index
         in order to compute the moving averages of values.
        :param value_tolerance: Stop sampling permutations after the first
         derivative of the means of the last min_steps values are within
         value_tolerance close to 0
        :param max_permutations: never run more than this many iterations (in
         total, across all workers: if num_workers = 100 and max_iterations =
         100
         then each worker will run at most one job)
        :return: Dict of approximated Shapley values for the indices

    """
    # if values is None:
    values = {i: [0.0] for i in data.index}
    # if converged_history is None:
    converged_history = []

    model.fit(data.x_train, data.y_train.values.ravel())
    _scores = []
    for _ in trange(bootstrap_iterations, desc="Bootstrapping"):
        sample = np.random.choice(data.x_test.index, len(data.x_test.index),
                                  replace=True)
        _scores.append(model.score(data.x_test.iloc[sample],
                                   data.y_test.iloc[sample].values.ravel()))
    global_score = float(np.mean(_scores))
    score_tolerance *= np.std(_scores)
    # import matplotlib.pyplot as plt
    # plt.hist(_scores, bins=40)
    # plt.savefig("bootstrap.png")

    num_samples = len(data)

    tasks_q = mp.Queue()
    results_q = mp.Queue()
    converged = mp.Value('i', 0)

    workers = [ShapleyWorker(i + 1,
                             tasks_q, results_q, converged, model, global_score,
                             data, min_samples, score_tolerance,
                             progress_bar=worker_progress)
               for i in range(num_workers)]

    # Fill the queue before starting the workers or they will immediately quit
    for _ in range(2 * num_workers):
        tasks_q.put(np.random.permutation(data.index))

    for w in workers:
        w.start()

    iteration = 1

    def get_process_result(it: int, timeout: float=None):
        permutation, scores, early_stop = results_q.get(timeout=timeout)
        for j, s, p in zip(permutation, scores[1:], scores[:-1]):
            values[j].append((it - 1) / it * values[j][-1] + (s - p) / it)

    pbar = tqdm(total=num_samples, position=0, desc=f"Run {run_id}. Converged")
    while converged.value < num_samples and iteration <= max_permutations:
        get_process_result(iteration)
        tasks_q.put(np.random.permutation(data.index))
        if iteration > min_values:
            converged.value = \
                vanishing_derivatives(np.array(list(values.values())),
                                      min_values=min_values,
                                      value_tolerance=value_tolerance)
            converged_history.append(converged.value)

        # converged.value can decrease. reset() clears times, but if just call
        # update(), the bar collapses. This is some hackery to fix that:
        pbar.n = converged.value
        pbar.last_print_n, pbar.last_print_t = converged.value, time()
        pbar.refresh()

        iteration += 1

    # Clear the queue of pending tasks
    try:
        while True:
            tasks_q.get_nowait()
    except queue.Empty:
        pass
    # Any workers still running won't post their results after the None task
    # has been placed...
    tasks_q.put(None)
    # ... But maybe someone put() some result while we were completing the last
    # iteration of the loop above:
    pbar.set_description_str("Gathering pending results")
    pbar.total = len(workers)
    pbar.reset()
    try:
        tmp = iteration
        while True:
            get_process_result(iteration, timeout=1.0)
            iteration += 1
            pbar.update(iteration - tmp)
    except queue.Empty:
        pass
    pbar.close()

    # results_q should be empty now
    assert results_q.empty(), \
        f"WTF? {results_q.qsize()} pending results"
    # HACK: the peeking in workers might empty the queue temporarily between the
    #  peeking and the restoring temporarily, so we allow for some timeout.
    assert tasks_q.get(timeout=0.1) is None, \
        f"WTF? {tasks_q.qsize()} pending tasks"

    for w in workers:
        w.join()
        w.close()

    return sort_values_history(values), converged_history


################################################################################
# TODO: Legacy implementations. Remove after thoroughly testing the one above.


def serial_montecarlo_shapley(model: SupervisedModel,
                              data: Dataset,
                              bootstrap_iterations: int,
                              min_samples: int,
                              score_tolerance: float,
                              min_steps: int,
                              value_tolerance: float,
                              max_iterations: int,
                              values: Dict[int, List[float]] = None,
                              converged_history: List[int] = None) \
        -> Tuple[OrderedDict, List[int]]:
    """ MonteCarlo approximation to the Shapley value of data points using
    only one CPU.

    Instead of naively implementing the expectation, we sequentially add points
    to a dataset from a permutation. We compute scores and stop when model
    performance doesn't increase beyond a threshold.

    We keep sampling permutations and updating all values until the change in
    the moving average for all values falls below another threshold.

        :param model: sklearn model / pipeline
        :param data: split Dataset
        :param bootstrap_iterations: Repeat global_score computation this many
         times to estimate variance.
        :param min_samples: Use so many of the last samples for a permutation
         in order to compute the moving average of scores.
        :param score_tolerance: For every permutation, computation stops
         after the mean increase in performance is within score_tolerance*stddev
         of the bootstrapped score over the **test** set (i.e. we bootstrap to
         compute variance of scores, then use that to stop)
        :param min_steps: complete at least these many value computations for
         every index and use so many of the last values for each sample index
         in order to compute the moving averages of values.
        :param value_tolerance: Stop sampling permutations after the first
         derivative of the means of the last min_steps values are within
         value_tolerance close to 0
        :param max_iterations: never run more than these many iterations
        :param values: Use these as starting point (retake computation)
        :param converged_history: Append to this history
        :return: Dict of approximated Shapley values for the indices

    """
    if values is None:
        values = {i: [0.0] for i in data.index}

    if converged_history is None:
        converged_history = []

    model.fit(data.x_train, data.y_train.values.ravel())
    _scores = []
    for _ in trange(bootstrap_iterations, desc="Bootstrapping"):
        sample = np.random.choice(data.x_test.index, len(data.x_test.index),
                                  replace=True)
        _scores.append(
                model.score(data.x_test.iloc[sample],
                            data.y_test.iloc[sample].values.ravel()))
    global_score = np.mean(_scores)
    score_tolerance *= np.std(_scores)

    iteration = 0
    converged = 0
    num_samples = len(data)
    pbar = tqdm(total=num_samples, position=1, desc=f"MCShapley")
    while iteration < min_steps \
            or (converged < num_samples and iteration < max_iterations):
        permutation = np.random.permutation(data.index)

        # FIXME: need score for model fitted on empty dataset
        scores = np.zeros(len(permutation) + 1)
        pbar.reset()
        for j, index in enumerate(permutation):
            pbar.set_description_str(
                    f"Iteration {iteration}, converged {converged}: ")

            # Stop if last min_samples have an average mean below threshold:
            last_scores = scores[
                          max(j - min_samples, 0):j].mean() if j > 0 else 0.0
            pbar.set_postfix_str(
                    f"last {min_samples} scores: {last_scores:.2e}")
            pbar.update()
            if abs(global_score - last_scores) < score_tolerance:
                scores[j + 1] = scores[j]
            else:
                x = data.x_train.iloc[permutation[:j + 1]]
                y = data.y_train.iloc[permutation[:j + 1]]
                model.fit(x, y.values.ravel())
                scores[j + 1] = model.score(data.x_test,
                                            data.y_test.values.ravel())
            # Update mean value: mean_n = mean_{n-1} + (new_val - mean_{n-1})/n
            # FIXME: is this correct?
            values[permutation[j]].append(
                    values[permutation[j]][-1]
                    + (scores[j + 1] - values[permutation[j]][-1]) / (j + 1))

        # Check empirical convergence of means (1st derivative ~= 0)
        # last_values = np.array(list(values.values()))[:, - (min_steps + 1):]
        # d = np.diff(last_values, axis=1)
        # converged = np.isclose(d, 0.0, atol=value_tolerance).sum()
        converged = \
            vanishing_derivatives(np.array(list(values.values())),
                                  min_values=min_steps,
                                  value_tolerance=value_tolerance)
        converged_history.append(converged)
        iteration += 1
        pbar.refresh()

    pbar.close()

    return sort_values_history(values), converged_history


def naive_montecarlo_shapley(model: SupervisedModel,
                             utility: Callable[[np.ndarray, np.ndarray], float],
                             data: Dataset,
                             indices: List[int],
                             max_iterations: int,
                             tolerance: float = None,
                             job_id: int = 0) \
        -> Tuple[OrderedDict, List[int]]:
    """ MonteCarlo approximation to the Shapley value of data points.

    This is a direct translation of the formula:

        Φ_i = E_π[ V(S^i_π ∪ {i}) - V(S^i_π) ],

    where π~Π is a draw from all possible n-permutations and S^i_π is the set
    of all indices before i in permutation π.

    As such it cannot take advantage of diminishing returns as an early stopping
    mechanism, hence the prefix "naive".

    Usage example
    =============
    n = len(x_train)
    chunk_size = 1 + int(n / num_jobs)
    indices = [[x_train.index[j] for j in range(i, min(i + chunk_size, n))]
               for i in range(0, n, chunk_size)]
    # NOTE: max_iterations should be a fraction of the number of permutations
    max_iterations = int(iterations_ratio * n)  # x% of training set size

    fun = partial(naive_montecarlo_shapley, model, model.score,
                  x_train, y_train, x_test, y_test,
                  max_iterations=max_iterations, tolerance=None)
    wrapped = parallel_wrap(fun, ("indices", indices), num_jobs=160)
    values, _ = run_and_gather(wrapped, num_jobs, num_runs)

    Arguments
    =========
        :param model: sklearn model / pipeline
        :param utility: utility function (e.g. any score function to maximise)
        :param data: split Dataset
        :param max_iterations: Set to e.g. len(x_train)/2: at 50% truncation the
            paper reports ~95% rank correlation with shapley values without
            truncation. (FIXME: dubious (by Hoeffding). Check the statement)
        :param tolerance: NOT IMPLEMENTED stop drawing permutations after delta
            in scores falls below this threshold
        :param job_id: for progress bar positioning
        :return: Dict of approximated Shapley values for the indices and dummy
                 list to conform to the generic interface in valuation.parallel
    """
    if tolerance is not None:
        raise NotImplementedError("Tolerance not implemented")

    values = {i: 0.0 for i in indices}

    for i in indices:
        pbar = trange(max_iterations, position=job_id, desc=f"Index {i}")
        mean_score = 0.0
        scores = []
        for _ in pbar:
            mean_score = np.nanmean(scores) if scores else 0.0
            pbar.set_postfix_str(f"mean: {mean_score:.2f}")
            permutation = np.random.permutation(data.index)
            # yuk... does not stop after match
            loc = np.where(permutation == i)[0][0]

            # HACK: some steps in a preprocessing pipeline might fail when there
            #   are too few rows. We set those as failures.

            try:
                model.fit(data.x_train.iloc[permutation[:loc + 1]],
                          data.y_train.iloc[permutation[:loc + 1]].values.ravel())
                score_with = utility(data.x_test, data.y_test.values.ravel())
                model.fit(data.x_train.iloc[permutation[:loc]],
                          data.y_train.iloc[permutation[:loc]].values.ravel())
                score_without = utility(data.x_test, data.y_test.values.ravel())
                scores.append(score_with - score_without)
            except:
                scores.append(np.nan)
        values[i] = mean_score
    return sort_values_history(values), []

