#  This file is part of Pynguin.
#
#  SPDX-FileCopyrightText: 2019–2020 Pynguin Contributors
#
#  SPDX-License-Identifier: LGPL-3.0-or-later
#
"""Provides a whole-suite test generation algorithm similar to EvoSuite."""
import logging
from typing import List, Tuple

import pynguin.configuration as config
import pynguin.ga.fitnessfunctions.branchdistancecasefitness as bdcf
import pynguin.ga.testcasechromosome as tcc
import pynguin.ga.testcasechromosomefactory as tccf
import pynguin.ga.testcasefactory as tcf
import pynguin.testsuite.testsuitechromosome as tsc
from pynguin.ga.operators.crossover.crossover import CrossOverFunction
from pynguin.ga.operators.crossover.singlepointrelativecrossover import (
    SinglePointRelativeCrossOver,
)
from pynguin.ga.operators.selection.rankselection import RankSelection
from pynguin.ga.operators.selection.selection import SelectionFunction
from pynguin.generation.algorithms.testgenerationstrategy import TestGenerationStrategy
from pynguin.setup.testcluster import TestCluster
from pynguin.testcase.execution.testcaseexecutor import TestCaseExecutor
from pynguin.utils import randomness
from pynguin.utils.exceptions import ConstructionFailedException
from pynguin.utils.statistics.statistics import RuntimeVariable, StatisticsTracker


# pylint: disable=too-few-public-methods
class WholeTestTestStrategy(TestGenerationStrategy):
    """Implements a whole-suite test generation algorithm similar to EvoSuite."""

    _logger = logging.getLogger(__name__)

    def __init__(self, executor: TestCaseExecutor, test_cluster: TestCluster) -> None:
        super().__init__(executor, test_cluster)
        self._chromosome_factory = tccf.TestCaseChromosomeFactory(
            self._test_factory, tcf.RandomLengthTestCaseFactory(self._test_factory)
        )
        self._population: List[tcc.TestCaseChromosome] = []
        self._selection_function: SelectionFunction[
            tcc.TestCaseChromosome
        ] = RankSelection()
        self._crossover_function: CrossOverFunction[
            tcc.TestCaseChromosome
        ] = SinglePointRelativeCrossOver()
        # self._fitness_functions = self.get_fitness_functions()
        self._fitness_functions = [
            bdcf.BranchDistanceCaseFitnessFunction(self._executor)
        ]

    def generate_sequences(
        self,
    ) -> Tuple[tsc.TestSuiteChromosome, tsc.TestSuiteChromosome]:
        stopping_condition = self.get_stopping_condition()
        stopping_condition.reset()
        self._population = self._get_random_population()
        self._sort_population()
        StatisticsTracker().current_individual(self._get_best_individual())
        generation = 0
        while (
            not self.is_fulfilled(stopping_condition)
            and self._get_best_individual().get_fitness() != 0.0
        ):
            self.evolve()
            StatisticsTracker().current_individual(self._get_best_individual())
            self._logger.info(
                "Generation: %5i. Best fitness: %5f, Best coverage %5f",
                generation,
                self._get_best_individual().get_fitness(),
                self._get_best_individual().get_coverage(),
            )
            generation += 1
        StatisticsTracker().track_output_variable(
            RuntimeVariable.AlgorithmIterations, generation
        )

        # wrap result to keep API.
        failing = tsc.TestSuiteChromosome()
        non_failing = tsc.TestSuiteChromosome()
        for chromosome in [failing, non_failing]:
            for fit_fun in self.get_fitness_functions():
                chromosome.add_fitness_function(fit_fun)

        best = self._get_best_individual()
        result = best.get_last_execution_result()
        assert result is not None
        if result.has_test_exceptions():
            failing.add_test_case_chromosome(best)
        else:
            non_failing.add_test_case_chromosome(best)

        return non_failing, failing

    def evolve(self) -> None:
        """Evolve the current population and replace it with a new one."""
        new_generation = []
        new_generation.extend(self.elitism())
        while not self.is_next_population_full(new_generation):
            parent1 = self._selection_function.select(self._population, 1)[0]
            parent2 = self._selection_function.select(self._population, 1)[0]

            offspring1 = parent1.clone()
            offspring2 = parent2.clone()

            try:
                if randomness.next_float() <= config.INSTANCE.crossover_rate:
                    self._crossover_function.cross_over(offspring1, offspring2)

                offspring1.mutate()
                offspring2.mutate()
            except ConstructionFailedException as ex:
                self._logger.info("Crossover/Mutation failed: %s", ex)
                continue

            fitness_parents = min(parent1.get_fitness(), parent2.get_fitness())
            fitness_offspring = min(offspring1.get_fitness(), offspring2.get_fitness())
            length_parents = parent1.length() + parent2.length()
            length_offspring = offspring1.length() + offspring2.length()
            best_individual = self._get_best_individual()

            if (fitness_offspring < fitness_parents) or (
                fitness_offspring == fitness_parents
                and length_offspring <= length_parents
            ):
                for offspring in [offspring1, offspring2]:
                    if offspring.length() <= 2 * best_individual.length():
                        new_generation.append(offspring)
                    else:
                        new_generation.append(randomness.choice([parent1, parent2]))
            else:
                new_generation.append(parent1)
                new_generation.append(parent2)

        self._population = new_generation
        self._sort_population()
        StatisticsTracker().current_individual(self._get_best_individual())

    def _get_random_population(self) -> List[tcc.TestCaseChromosome]:
        population = []
        for _ in range(config.INSTANCE.population):
            chromosome = self._chromosome_factory.get_chromosome()
            for fitness_function in self._fitness_functions:
                chromosome.add_fitness_function(fitness_function)
            population.append(chromosome)
        return population

    def _sort_population(self) -> None:
        """Sort the population by fitness."""
        self._population.sort(key=lambda x: x.get_fitness())

    def _get_best_individual(self) -> tcc.TestCaseChromosome:
        """Get the currently best individual.

        Returns:
            The best chromosome
        """
        return self._population[0]

    @staticmethod
    def is_next_population_full(population: List[tcc.TestCaseChromosome]) -> bool:
        """Check if the population is already full.

        Args:
            population: The list of chromosomes, i.e., the population

        Returns:
            Whether or not the population is already full
        """
        return len(population) >= config.INSTANCE.population

    def elitism(self) -> List[tcc.TestCaseChromosome]:
        """Copy best individuals.

        Returns:
            A list of the best chromosomes
        """
        elite = []
        for idx in range(config.INSTANCE.elite):
            elite.append(self._population[idx].clone())
        return elite
