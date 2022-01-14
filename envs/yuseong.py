'''
import configparser
import logging
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import os
import seaborn as sns
import time
from envs.env import PhaseMap, PhaseSet, TrafficSimulator
from small_grid.data.build_file import gen_rou_file
'''

import pickle
import pandas as pd
import win32com.client as com
import os
from envs.env import PhaseMap, PhaseSet, TrafficSimulator

YUSEONG_NEIGHBOR_MAP = {'nt1': ['npc', 'nt2', 'nt6'],
                           'nt2': ['nt1', 'nt3'],
                           'nt3': ['npc', 'nt2', 'nt4'],
                           'nt4': ['nt3', 'nt5'],
                           'nt5': ['npc', 'nt4', 'nt6'],
                           'nt6': ['nt1', 'nt5']}

STATE_NAMES = ['wave', 'wait']

class YuseongPhase(PhaseMap):
    def __init__(self):
        two_phase = ['GGrr', 'rrGG']
        three_phase = ['GGGrrrrrr', 'rrrGGGrrr', 'rrrrrrGGG']
        self.phases = {2: PhaseSet(two_phase), 3: PhaseSet(three_phase)}


class YuseongController:
    def __init__(self, node_names):
        self.name = 'greedy'
        self.node_names = node_names

    def forward(self, obs):
        actions = []
        for ob, node_name in zip(obs, self.node_names):
            actions.append(self.greedy(ob, node_name))
        return actions

    #def greedy(self, ob, node_name):
    #    # hard code the mapping from state to number of cars
    #    phases = STATE_PHASE_MAP[node_name]
    #    flows = ob[:len(phases)]
    #    return phases[np.argmax(flows)]


class YuseongEnv(TrafficSimulator):
    def __init__(self, config, port = 0, output_path = '', is_record = False, record_stat=False):
        self.num_car_hourly = config.getint('num_extra_car_per_hour')
        super().__init__(config, output_path, is_record, record_stat, port=port)

    def _init_map(self):

        self.neighbor_map = YUSEONG_NEIGHBOR_MAP
        self.phase_map = YuseongPhase()
        self.state_names = STATE_NAMES



    def vehicle_input(self):
        pass

