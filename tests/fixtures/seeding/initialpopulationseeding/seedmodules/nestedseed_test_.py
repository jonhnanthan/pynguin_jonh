#  This file is part of Pynguin.
#
#  SPDX-FileCopyrightText: 2019–2021 Pynguin Contributors
#
#  SPDX-License-Identifier: LGPL-3.0-or-later
#
import tests.fixtures.seeding.initialpopulationseeding.dummycontainer as module0


def seed_test_case():
    var0 = 10
    var1 = {var0, "test", -2, [1, 2], {1: True}, list([1, 2, 3])}
    var2 = module0.i_take_set(s=var1)
    assert var2 == "not empty!"
