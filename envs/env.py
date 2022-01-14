"""
Traffic network simulator w/ defined sumo files
@author: Tianshu Chu
"""
import logging
import numpy as np
import pandas as pd
import subprocess
#from sumolib import checkBinary
import time
#import traci
import xml.etree.cElementTree as ET

import pickle
import win32com.client as com
import os

DEFAULT_PORT = 8000
SEC_IN_MS = 1000

# hard code real-net reward norm
REALNET_REWARD_NORM = 20

class PhaseSet:
    def __init__(self, phases):
        self.num_phase = len(phases)
        self.num_lane = len(phases[0])
        self.phases = phases
        # self._init_phase_set()

    @staticmethod
    def _get_phase_lanes(phase, signal='r'):
        phase_lanes = []
        for i, l in enumerate(phase):
            if l == signal:
                phase_lanes.append(i)
        return phase_lanes

    def _init_phase_set(self):
        self.red_lanes = []
        # self.green_lanes = []
        for phase in self.phases:
            self.red_lanes.append(self._get_phase_lanes(phase))
            # self.green_lanes.append(self._get_phase_lanes(phase, signal='G'))


class PhaseMap:
    def __init__(self):
        self.phases = {}

    def get_phase(self, phase_id, action):
        # phase_type is either green or yellow
        return self.phases[phase_id].phases[int(action)]

    def get_phase_num(self, phase_id):
        return self.phases[phase_id].num_phase

    def get_lane_num(self, phase_id):
        # the lane number is link number
        return self.phases[phase_id].num_lane

    def get_red_lanes(self, phase_id, action):
        # the lane number is link number
        return self.phases[phase_id].red_lanes[int(action)]


class Node:
    def __init__(self, name, neighbor=[], control=False):
        self.control = control # disabled
        self.edges_in = []  # for reward
        self.lanes_in = []
        self.ilds_in = [] # for state
        self.fingerprint = [] # local policy
        self.name = name
        self.neighbor = neighbor
        self.num_state = 0 # wave and wait should have the same dim
        self.num_fingerprint = 0
        self.wave_state = [] # local state
        self.wait_state = [] # local state
        self.waits = []
        self.phase_id = -1
        self.n_a = 0
        self.prev_action = -1


class TrafficSimulator:
    def __init__(self, config, output_path, is_record, record_stats, port=0):
        ## <Section: ENV_CONFIG>      False   False

        self.name = config.get('scenario')  ## yuseong
        self.seed = config.getint('seed')   ## 12
        self.control_interval_sec = config.getint('control_interval_sec')  ## 5
        self.yellow_interval_sec = config.getint('yellow_interval_sec')  ## 2
        self.episode_length_sec = config.getint('episode_length_sec')  ## 3600
        self.T = np.ceil(self.episode_length_sec / self.control_interval_sec)
        self.port = DEFAULT_PORT + port
        self.sim_thread = port
        self.obj = config.get('objective')      ## hyrid
        self.data_path = config.get('data_path')  ## ./large_grid/data
        self.agent = config.get('agent')  ## ia2c
        self.coop_gamma = config.getfloat('coop_gamma') ## 0.9
        self.cur_episode = 0
        self.norms = {'wave': config.getfloat('norm_wave'),  ##5
                      'wait': config.getfloat('norm_wait')}  ##100
        self.clips = {'wave': config.getfloat('clip_wave'),  ##2
                      'wait': config.getfloat('clip_wait')}  ##2
        self.coef_wait = config.getfloat('coef_wait')  ##0.2
        self.train_mode = True
        test_seeds = config.get('test_seeds').split(',')  ## 10000, 20000
        test_seeds = [int(s) for s in test_seeds]

        self._init_map()
        self.init_data(is_record, record_stats, output_path)
        self.init_test_seeds(test_seeds)
        self._init_sim(self.seed) ## VISSIM setting
        self._init_nodes()  ## SignalController + road setting
        #self.terminate()
        #print("VISSIM setting finish")


    def veh_input(self):
        with open(f'./input data/{self.name}_traffic.pickle', 'rb') as f:
            df = pickle.load(f)
        df = df.rename(dict(zip(list(df.index), list(range(1, len(df) + 1)))))

        # CCTV id 2 records traffic from West
        CCTV_DIR = {1: 'S', 2: 'W', 3: 'N', 4: 'E'}

        # direction to routing decision id mapping
        DIR_SVRD = {'N': 4, 'S': 3, 'E': 2, 'W': 1}

        # link id to vehicle input id
        DIR_VI = {'E': 1, 'N': 2, 'W': 3, 'S': 4}

        # link id to vehicle composition id
        DIR_VC = {'N': 1, 'E': 2, 'S': 3, 'W': 4}

        # For direction ordering
        dir_tuple = ('RIGHT_TRF_', 'GO_TRF_', 'LEFT_TRF_')

        for i, record in df.iterrows():

            trf_by_cctv = {num: sum([int(record[d + str(num)]) for d in dir_tuple])
                           for num in range(1, 5)}

            for cctv_num, traffic in trf_by_cctv.items():
                dir = CCTV_DIR[cctv_num]
                vi_id = DIR_VI[dir]

                # Set total traffic volume for each time step
                if i > 1:
                    self.Vissim.Net.VehicleInputs.ItemByKey(vi_id).SetAttValue(f'Cont({i})', False)
                self.Vissim.Net.VehicleInputs.ItemByKey(vi_id).SetAttValue(f'Volume({i})', int(traffic))

                # Set Vehicle Routing Decision
                svrd_id = DIR_SVRD[dir]
                cctv_trf = [int(record[d + str(cctv_num)]) for d in dir_tuple]
                total = sum(cctv_trf)
                # 1: right, 2: straight, 3: left
                for svr_id, trf in zip([1, 2, 3], cctv_trf):
                    self.Vissim.Net.VehicleRoutingDecisionsStatic.ItemByKey(svrd_id).VehRoutSta.ItemByKey(
                        svr_id).SetAttValue(f'RelFlow({i})', trf / total)

        with open(f'./input data/{self.name}_weight.pickle', 'rb') as f:
            df2 = pickle.load(f)
        # print(df2)
        vc_type = ['CAR', 'BUS', 'BIKE']
        vc_id = [100, 300, 610]
        vc_speed = [50, 40, 40]

        df2 = df2.rename(dict(zip(list(df2.index), list(range(1, len(df2) + 1)))))

        for cctv_num in range(1, 5):
            dir = CCTV_DIR[cctv_num]
            vc_id = DIR_VC[dir]
            Rel_Flows = self.Vissim.Net.VehicleCompositions.ItemByKey(vc_id).VehCompRelFlows.GetAll()
            for i, type in enumerate(vc_type):
                # Rel_Flows[i].SetAttValue('VehType',        vc_id[i]) # Changing the vehicle type -> type subscriptable 오류
                Rel_Flows[i].SetAttValue('DesSpeedDistr', vc_speed[i])  # Changing the desired speed distribution
                Rel_Flows[i].SetAttValue('RelFlow', df2.loc[cctv_num][type])  # Changing the relative flow

    def signal(self):

        self.SC_number = 1  # SC = SignalController
        self.SH = []
        self.SG = []
        ## ====== Signal Controller & Signal Head & Signal Group Setting ======
        # Set a signal controller program:
        self.SignalController = self.Vissim.Net.SignalControllers.ItemByKey(self.SC_number)
        for i in range(16):
            self.SH.append(self.Vissim.Net.SignalHeads.ItemByKey(i + 1).AttValue('SigState'))
        for i in range(8):
            self.SG.append(self.SignalController.SGs.ItemByKey(i + 1))

    def road(self):
        Input = {'9-1': 1, '9-2': 1, '9-3': 2, '9-4': 2, '19-2': 3, '19-3': 3, '10025-2': 3, '10025-3': 3, '20-2': 3, '20-3': 3,
                 '19-4': 4, '19-5': 4, '10024': 4, '10025-4': 4, '20-4': 4, '2-2': 5, '2-3': 5, '1-2': 5, '1-3': 5,
                 '2-4': 6, '2-5': 6, '1-4': 6, '1-5': 6, '13-2': 7, '13-3': 7, '13-4': 7, '10013-2': 7,'10013-3': 7, '10013-4': 7,
                 '13-5': 8, '10030': 8}
        find = [9,19,20,2,1,13,10025,10024,10013,10030]

        self.lane = {'1' : [], '2':[],'3':[],'4':[],'5':[],'6':[],'7':[],'8':[]}

        for link in self.Vissim.Net.Links:
            if int(link.AttValue('No')) in find:
                for lane in link.Lanes.GetAll():
                    temp = str(link.AttValue('No')) + '-' + str(lane.AttValue('Index'))
                    if temp in Input : self.lane[str(Input[temp])].append(lane)

        return self.lane

    def _debug_traffic_step(self):
        for node_name in self.node_names:
            node = self.nodes[node_name]
            phase = self.sim.trafficlight.getRedYellowGreenState(self.node_names[0])
            cur_traffic = {'episode': self.cur_episode,
                           'time_sec': self.cur_sec,
                           'node': node_name,
                           'action': node.prev_action,
                           'phase': phase}
            for i, ild in enumerate(node.ilds_in):
                cur_name = 'lane%d_' % i
                cur_traffic[cur_name + 'queue'] = self.sim.lane.getLastStepHaltingNumber(ild)
                cur_traffic[cur_name + 'flow'] = self.sim.lane.getLastStepVehicleNumber(ild)
                # cur_traffic[cur_name + 'wait'] = node.waits[i]
            self.traffic_data.append(cur_traffic)

    def _get_node_phase(self, action, node_name, phase_type):
        node = self.nodes[node_name]
        cur_phase = self.phase_map.get_phase(node.phase_id, action)
        if phase_type == 'green':
            return cur_phase
        prev_action = node.prev_action
        node.prev_action = action
        if (prev_action < 0) or (action == prev_action):
            return cur_phase
        prev_phase = self.phase_map.get_phase(node.phase_id, prev_action)
        switch_reds = []
        switch_greens = []
        for i, (p0, p1) in enumerate(zip(prev_phase, cur_phase)):
            if (p0 in 'Gg') and (p1 == 'r'):
                switch_reds.append(i)
            elif (p0 in 'r') and (p1 in 'Gg'):
                switch_greens.append(i)
        if not len(switch_reds):
            return cur_phase
        yellow_phase = list(cur_phase)
        for i in switch_reds:
            yellow_phase[i] = 'y'
        for i in switch_greens:
            yellow_phase[i] = 'r'
        return ''.join(yellow_phase)

    def _get_node_phase_id(self, node_name):
        # needs to be overwriteen
        raise NotImplementedError()

    def _get_node_state_num(self, node):
        assert len(node.lanes_in) == self.phase_map.get_lane_num(node.phase_id)
        # wait / wave states for each lane
        return len(node.ilds_in)

    def _get_state(self):



        # hard code the state ordering as wave, wait, fp
        state = []
        '''
        # measure the most recent state
        
        self._measure_state_step()

        # get the appropriate state vectors
        for node_name in self.node_names:
            node = self.nodes[node_name]
            # wave is required in state
            if self.agent == 'greedy':
                state.append(node.wave_state)
            elif self.agent == 'a2c':
                if 'wait' in self.state_names:
                    state.append(np.concatenate([node.wave_state, node.wait_state]))
                else:
                    state.append(node.wave_state)
            else:
                cur_state = [node.wave_state]
                # include wave states of neighbors
                for nnode_name in node.neighbor:
                    if self.agent != 'ma2c':
                        cur_state.append(self.nodes[nnode_name].wave_state)
                    else:
                        # discount the neigboring states
                        cur_state.append(self.nodes[nnode_name].wave_state * self.coop_gamma)
                # include wait state
                if 'wait' in self.state_names:
                    cur_state.append(node.wait_state)
                # include fingerprints of neighbors
                if self.agent == 'ma2c':
                    for nnode_name in node.neighbor:
                        cur_state.append(self.nodes[nnode_name].fingerprint)
                state.append(np.concatenate(cur_state))

        if self.agent == 'a2c':
            state = np.concatenate(state)

        # # clean up the state and fingerprint measurements
        # for node in self.node_names:
        #     self.nodes[node].state = np.zeros(self.nodes[node].num_state)
        #     self.nodes[node].fingerprint = np.zeros(self.nodes[node].num_fingerprint)
        '''
        #return state
        pass

    def _init_nodes(self):
        ## 모든 교차로에 대한 SC 정의, 인접한 이웃 교차로 설정, 교차로에 연결되어 있는 link 정의

        nodes = {}
        self.SC = {}
        for index, SC_ID in enumerate (self.Vissim.Net.SignalControllers) :
            print("SC_ID : ", SC_ID)
            self.SC['nt' + str(int(index)+1)] = SC_ID


        for node_name in self.Vissim.Net.Nodes :
            if node_name.AttValue('name') in self.neighbor_map:
                neighbor = self.neighbor_map[node_name]
            else :
                neighbor = []
            nodes[node_name] = Node(node_name, neighbor=neighbor, control=True)

            SGs = self.SC[node_name.AttValue('name')].SGs
            print("SGs : ", SGs)
            #lanes_in =

        print("nodes dict")
        print(nodes)



        '''
        self.SC_number = 1
        node_name =    self.Vissim.Net.SignalControllers.ItemByKey(self.SC_number)
        nodes = Node(node_name, neighbor=[], control=True)
        #nodes[node_name] = Node(node_name, neighbor=[], control=True)

        #lanes_in = self.road()
        lanes_in = []
        for link in self.Vissim.Net.Links: lanes_in.append(link)
        nodes.lanes_in = lanes_in

        ilds_in = []
        for lane_name in lanes_in:
            ild_name = lane_name
            if ild_name not in ilds_in:
                ilds_in.append(ild_name)

        nodes.ilds_in = ilds_in
        self.nodes = nodes
        self.node_name = node_name
        #self.node_names = sorted(list(nodes.keys()))

        self._init_action_space()
        self._init_state_space()

        '''
        print("init_node : ", self.neighbor_map)
        '''
        for node_name in self.sim.trafficlight.getIDList():
            if node_name in self.neighbor_map:
                neighbor = self.neighbor_map[node_name]
            else:
                logging.info('node %s can not be found!' % node_name)
                neighbor = []
            nodes[node_name] = Node(node_name,
                                    neighbor=neighbor,
                                    control=True)
            # controlled lanes: l:j,i_k
            lanes_in = self.sim.trafficlight.getControlledLanes(node_name)
            nodes[node_name].lanes_in = lanes_in
            
            # controlled edges: e:j,i
            # lane ilds: ild:j,i_k for road ji, lane k.
            # edges_in = []
            ilds_in = []
            for lane_name in lanes_in:
                ild_name = lane_name
                if ild_name not in ilds_in:
                    ilds_in.append(ild_name)
            # nodes[node_name].edges_in = edges_in
            nodes[node_name].ilds_in = ilds_in
        
        self.nodes = nodes
        self.node_names = sorted(list(nodes.keys()))
        
        
        s = 'Env: init %d node information:\n' % len(self.node_names)
        for node in self.nodes.values():
            s += node.name + ':\n'
            s += '\tneigbor: %r\n' % node.neighbor
            # s += '\tlanes_in: %r\n' % node.lanes_in
            s += '\tilds_in: %r\n' % node.ilds_in
            # s += '\tedges_in: %r\n' % node.edges_in
        
        logging.info(s)
        '''


    def _init_action_space(self):
        # for local and neighbor coop level
        #node = self.nodes
        #phase_id = 'yuseong4'
        #node.phase_id = phase_id
        #node.n_a = self.phase_map.get_phase_num(phase_id)
        self.n_a = 30
        self.n_a = np.prod(np.array(self.n_a_ls))

        '''
        def get_phase_num(self, phase_id):
         return self.phases[phase_id].num_phase
        
        self.n_a_ls = []
        
        for node_name in self.node_names:
            node = self.nodes[node_name]
            phase_id = self._get_node_phase_id(node_name)
            node.phase_id = phase_id
            node.n_a = self.phase_map.get_phase_num(phase_id)
            self.n_a_ls.append(node.n_a)
        # for global coop level
        '''

        #self.n_a = np.prod(np.array(self.n_a_ls))

    def _init_state_space(self):
        self._reset_state()
        self.n_s_ls = []
        self.n_w_ls = []
        self.n_f_ls = []

        num_wave = self.nodes.num_state
        num_fingerprint = 0
        num_wave += self.nodes[nnode_name].num_state
        num_wait = 0 if 'wait' not in self.state_names else node.num_state

        self.n_s_ls.append(num_wave + num_wait + num_fingerprint)
        self.n_f_ls.append(num_fingerprint)
        self.n_w_ls.append(num_wait)

        self.n_s = np.sum(np.array(self.n_s_ls))
        '''
        for node_name in self.node_names:
                node = self.nodes[node_name]
                num_wave = node.num_state
                num_fingerprint = 0
                for nnode_name in node.neighbor:
                    if self.agent not in ['a2c', 'greedy']:
                        # all marl agents have neighborhood communication
                        num_wave += self.nodes[nnode_name].num_state
                    if self.agent == 'ma2c':
                        # only ma2c uses neighbor's policy
                        num_fingerprint += self.nodes[nnode_name].num_fingerprint
                num_wait = 0 if 'wait' not in self.state_names else node.num_state
                self.n_s_ls.append(num_wave + num_wait + num_fingerprint)
                self.n_f_ls.append(num_fingerprint)
                self.n_w_ls.append(num_wait)
            self.n_s = np.sum(np.array(self.n_s_ls))
            '''
    def _init_map(self):
        # needs to be overwriteen
        self.neighbor_map = None
        self.phase_map = None
        self.state_names = None
        raise NotImplementedError()

    def _init_policy(self):
        policy = []
        for node_name in self.node_names:
            phase_num = self.nodes[node_name].n_a
            p = 1. / phase_num
            policy.append(np.array([p] * phase_num))
        return policy

    def _init_sim(self, seed, gui=False):

        ## =================VISSIM setting==============

        self.Vissim = com.gencache.EnsureDispatch("Vissim.Vissim")
        cwd = os.getcwd()

        Filename = os.path.join(cwd, f'{self.name}.inpx')

        flag_read_additionally = False
        self.Vissim.LoadNet(Filename, flag_read_additionally)

        self.Vissim.Simulation.SetAttValue('SimSpeed', 1)

        ## ===============================
        #self.veh_input()
        #self.signal()
        #self.road()
        ## 이전 시뮬레이션 종료 + 약 10초간 phase없이 돌리기==========================================================

        for simRun in Vissim.Net.SimulationRuns:
            self.Vissim.Net.SimulationRuns.RemoveSimulationRun(simRun)

        #self.veh_input()  ## 차량 입력 // 추후에는 parameter를 사용하여 입력데이터 다양하게

        #for  _ in range(100):
        #    self.Vissim.Simulation.RunSingleStep()



        ''''
        sumocfg_file = self._init_sim_config(seed)
        if gui:
            app = 'sumo-gui'
        else:
            app = 'sumo'
        command = [checkBinary(app), '-c', sumocfg_file]
        command += ['--seed', str(seed)]
        command += ['--remote-port', str(self.port)]
        command += ['--no-step-log', 'True']
        if self.name != 'real_net':
            command += ['--time-to-teleport', '600'] # long teleport for safety
        else:
            command += ['--time-to-teleport', '300']
        command += ['--no-warnings', 'True']
        command += ['--duration-log.disable', 'True']
        # collect trip info if necessary
        if self.is_record:
            command += ['--tripinfo-output',
                        self.output_path + ('%s_%s_trip.xml' % (self.name, self.agent))]
        print("command : ", command)
        subprocess.Popen(command)
        # wait 2s to establish the traci server
        time.sleep(2)
        self.sim = traci.connect(port=self.port)
        '''

    def _init_sim_config(self):
        # needs to be overwriteen
        raise NotImplementedError()

    def _init_sim_traffic(self):
        return

    def _measure_reward_step(self):
        rewards = []
        for node_name in self.node_names:
            queues = []
            waits = []
            for ild in self.nodes[node_name].ilds_in:
                if self.obj in ['queue', 'hybrid']:
                    if self.name == 'real_net':
                        cur_queue = min(10, self.sim.lane.getLastStepHaltingNumber(ild))
                    else:
                        cur_queue = self.sim.lanearea.getLastStepHaltingNumber(ild)
                    queues.append(cur_queue)
                if self.obj in ['wait', 'hybrid']:
                    max_pos = 0
                    car_wait = 0
                    if self.name == 'real_net':
                        cur_cars = self.sim.lane.getLastStepVehicleIDs(ild)
                    else:
                        cur_cars = self.sim.lanearea.getLastStepVehicleIDs(ild)
                    for vid in cur_cars:
                        car_pos = self.sim.vehicle.getLanePosition(vid)
                        if car_pos > max_pos:
                            max_pos = car_pos
                            car_wait = self.sim.vehicle.getWaitingTime(vid)
                    waits.append(car_wait)
                # if self.name == 'real_net':
                #     lane_name = ild.split(':')[1]
                # else:
                #     lane_name = 'e:' + ild.split(':')[1]
                # queues.append(self.sim.lane.getLastStepHaltingNumber(lane_name))

            queue = np.sum(np.array(queues)) if len(queues) else 0
            wait = np.sum(np.array(waits)) if len(waits) else 0
            # if self.obj in ['wait', 'hybrid']:
            #     wait = np.sum(self.nodes[node_name].waits * (queues > 0))
            if self.obj == 'queue':
                reward = - queue
            elif self.obj == 'wait':
                reward = - wait
            else:
                reward = - queue - self.coef_wait * wait
            rewards.append(reward)
        return np.array(rewards)

    def _measure_state_step(self):
        for node_name in self.node_names:
            node = self.nodes[node_name]
            for state_name in self.state_names:
                if state_name == 'wave':
                    cur_state = []
                    for ild in node.ilds_in:
                        if self.name == 'real_net':
                            cur_wave = self.sim.lane.getLastStepVehicleNumber(ild)
                        else:
                            cur_wave = self.sim.lanearea.getLastStepVehicleNumber(ild)
                        cur_state.append(cur_wave)
                    cur_state = np.array(cur_state)
                else:
                    cur_state = []
                    for ild in node.ilds_in:
                        max_pos = 0
                        car_wait = 0
                        if self.name == 'real_net':
                            cur_cars = self.sim.lane.getLastStepVehicleIDs(ild)
                        else:
                            cur_cars = self.sim.lanearea.getLastStepVehicleIDs(ild)
                        for vid in cur_cars:
                            car_pos = self.sim.vehicle.getLanePosition(vid)
                            if car_pos > max_pos:
                                max_pos = car_pos
                                car_wait = self.sim.vehicle.getWaitingTime(vid)
                        cur_state.append(car_wait)
                    cur_state = np.array(cur_state)
                if self.record_stats:
                    self.state_stat[state_name] += list(cur_state)
                # normalization
                norm_cur_state = self._norm_clip_state(cur_state,
                                                       self.norms[state_name],
                                                       self.clips[state_name])
                if state_name == 'wave':
                    node.wave_state = norm_cur_state
                else:
                    node.wait_state = norm_cur_state

    def _measure_traffic_step(self):
        cars = self.sim.vehicle.getIDList()
        num_tot_car = len(cars)
        num_in_car = self.sim.simulation.getDepartedNumber()
        num_out_car = self.sim.simulation.getArrivedNumber()
        if num_tot_car > 0:
            avg_waiting_time = np.mean([self.sim.vehicle.getWaitingTime(car) for car in cars])
            avg_speed = np.mean([self.sim.vehicle.getSpeed(car) for car in cars])
        else:
            avg_speed = 0
            avg_waiting_time = 0
        # all trip-related measurements are not supported by traci,
        # need to read from outputfile afterwards
        queues = []
        for node_name in self.node_names:
            for ild in self.nodes[node_name].ilds_in:
                queues.append(self.sim.lane.getLastStepHaltingNumber(ild))
        avg_queue = np.mean(np.array(queues))
        std_queue = np.std(np.array(queues))
        cur_traffic = {'episode': self.cur_episode,
                       'time_sec': self.cur_sec,
                       'number_total_car': num_tot_car,
                       'number_departed_car': num_in_car,
                       'number_arrived_car': num_out_car,
                       'avg_wait_sec': avg_waiting_time,
                       'avg_speed_mps': avg_speed,
                       'std_queue': std_queue,
                       'avg_queue': avg_queue}
        self.traffic_data.append(cur_traffic)

    @staticmethod
    def _norm_clip_state(x, norm, clip=-1):
        x = x / norm
        return x if clip < 0 else np.clip(x, 0, clip)

    def _reset_state(self):

        self.nodes.prev_action = 0
        self.nodes.num_fingerprint = self.nodes.n_a - 1
        self.nodes.num_state = self._get_node_state_num(self.nodes)
        '''
        for node_name in self.node_names:
            node = self.nodes[node_name]
            # prev action for yellow phase before each switch
            node.prev_action = 0
            # fingerprint is previous policy[:-1]
            node.num_fingerprint = node.n_a - 1
            node.num_state = self._get_node_state_num(node)
            # node.waves = np.zeros(node.num_state)
            # node.waits = np.zeros(node.num_state)
        '''

    def _set_phase(self, action, phase_type, phase_duration):


        '''
        for node_name, a in zip(self.node_names, list(action)):
            phase = self._get_node_phase(a, node_name, phase_type)
            self.sim.trafficlight.setRedYellowGreenState(node_name, phase)
            self.sim.trafficlight.setPhaseDuration(node_name, phase_duration)
        '''
    def _simulate(self, num_step):
        # reward = np.zeros(len(self.control_node_names))
        for _ in range(num_step):
            self.sim.simulationStep()
            # self._measure_state_step()
            # reward += self._measure_reward_step()
            self.cur_sec += 1
            if self.is_record:
                # self._debug_traffic_step()
                self._measure_traffic_step()
        # return reward

    def _transfer_action(self, action):
        '''Transfer global action to a list of local actions'''
        phase_nums = []
        for node in self.control_node_names:
            phase_nums.append(self.nodes[node].phase_num)
        action_ls = []
        for i in range(len(phase_nums) - 1):
            action, cur_action = divmod(action, phase_nums[i])
            action_ls.append(cur_action)
        action_ls.append(action)
        return action_ls

    def _update_waits(self, action):
        for node_name, a in zip(self.node_names, action):
            red_lanes = set()
            node = self.nodes[node_name]
            for i in self.phase_map.get_red_lanes(node.phase_id, a):
                red_lanes.add(node.lanes_in[i])
            for i in range(len(node.waits)):
                lane = node.ilds_in[i]
                if lane in red_lanes:
                    node.waits[i] += self.control_interval_sec
                else:
                    node.waits[i] = 0

    def collect_tripinfo(self):
        # read trip xml, has to be called externally to get complete file
        trip_file = self.output_path + ('%s_%s_trip.xml' % (self.name, self.agent))
        tree = ET.ElementTree(file=trip_file)
        for child in tree.getroot():
            cur_trip = child.attrib
            cur_dict = {}
            cur_dict['episode'] = self.cur_episode
            cur_dict['id'] = cur_trip['id']
            cur_dict['depart_sec'] = cur_trip['depart']
            cur_dict['arrival_sec'] = cur_trip['arrival']
            cur_dict['duration_sec'] = cur_trip['duration']
            cur_dict['wait_step'] = cur_trip['waitingCount']
            cur_dict['wait_sec'] = cur_trip['waitingTime']
            self.trip_data.append(cur_dict)
        # delete the current xml
        cmd = 'rm ' + trip_file
        subprocess.check_call(cmd, shell=True)

    def init_data(self, is_record, record_stats, output_path):
        self.is_record = is_record
        self.record_stats = record_stats
        #print("init_data : ", is_record, ' ', record_stats, ' ', output_path)
        self.output_path = output_path
        if self.is_record:
            self.traffic_data = []
            self.control_data = []
            self.trip_data = []
        if self.record_stats:
            self.state_stat = {}
            for state_name in self.state_names:
                self.state_stat[state_name] = []

    def init_test_seeds(self, test_seeds):
        self.test_num = len(test_seeds)
        self.test_seeds = test_seeds

    def output_data(self):
        if not self.is_record:
            logging.error('Env: no record to output!')
        control_data = pd.DataFrame(self.control_data)
        control_data.to_csv(self.output_path + ('%s_%s_control.csv' % (self.name, self.agent)))
        traffic_data = pd.DataFrame(self.traffic_data)
        traffic_data.to_csv(self.output_path + ('%s_%s_traffic.csv' % (self.name, self.agent)))
        trip_data = pd.DataFrame(self.trip_data)
        trip_data.to_csv(self.output_path + ('%s_%s_trip.csv' % (self.name, self.agent)))

    def reset(self, gui=False, test_ind=0):
        # have to terminate previous sim before calling reset
        self._reset_state()
        if self.train_mode:
            seed = self.seed
        else:
            seed = self.test_seeds[test_ind]

        ## vissim network에 어느정도 교통량 부과
        self._init_sim(seed, gui=gui)
        self.cur_sec = 0
        self.cur_episode += 1
        # initialize fingerprint
        if self.agent == 'ma2c':
            self.update_fingerprint(self._init_policy())
        #self._init_sim_traffic()
        # next environment random condition should be different
        self.seed += 1

        print("get_state 이전까지 완료")
        return self._get_state()

    def terminate(self):
        self.sim.close()

    def step(self, action):
        if self.agent == 'a2c':
            action = self._transfer_action(action)
        # self._update_waits(action)
        self._set_phase(action, 'yellow', self.yellow_interval_sec)
        self._simulate(self.yellow_interval_sec)
        rest_interval_sec = self.control_interval_sec - self.yellow_interval_sec
        self._set_phase(action, 'green', rest_interval_sec)
        self._simulate(rest_interval_sec)
        state = self._get_state()
        reward = self._measure_reward_step()
        done = False
        if self.cur_sec >= self.episode_length_sec:
            done = True
        global_reward = np.sum(reward) # for fair comparison
        if self.is_record:
            action_r = ','.join(['%d' % a for a in action])
            cur_control = {'episode': self.cur_episode,
                           'time_sec': self.cur_sec,
                           'step': self.cur_sec / self.control_interval_sec,
                           'action': action_r,
                           'reward': global_reward}
            self.control_data.append(cur_control)

        # use local rewards in test
        if not self.train_mode:
            return state, reward, done, global_reward
        if self.agent in ['a2c', 'greedy']:
            reward = global_reward
        elif self.agent != 'ma2c':
            # global reward is shared in independent rl
            new_reward = [global_reward] * len(reward)
            reward = np.array(new_reward)
            if self.name == 'real_net':
                # reward normalization in env for realnet
                reward = reward / (len(self.node_names) * REALNET_REWARD_NORM)
        else:
            # discounted global reward for ma2c
            new_reward = []
            for node_name, r in zip(self.node_names, reward):
                cur_reward = r
                for nnode_name in self.nodes[node_name].neighbor:
                    i = self.node_names.index(nnode_name)
                    cur_reward += self.coop_gamma * reward[i]
                # for i, nnode in enumerate(self.node_names):
                #     if nnode == node:
                #         continue
                #     if nnode in self.nodes[node].neighbor:
                #         cur_reward += self.coop_gamma * reward[i]
                #     elif self.name == 'small_grid':
                #         # in small grid, agent is at most 2 steps away
                #         cur_reward += (self.coop_gamma ** 2) * reward[i]
                #     else:
                #         # in large grid, a distance map is used
                #         if nnode in self.distance_map[node]:
                #             distance = self.distance_map[node][nnode]
                #             cur_reward += (self.coop_gamma ** distance) * reward[i]
                #         else:
                #             cur_reward += (self.coop_gamma ** self.max_distance) * reward[i]
                if self.name != 'real_net':
                    new_reward.append(cur_reward)
                else:
                    n_node = 1 + len(self.nodes[node_name].neighbor)
                    new_reward.append(cur_reward / (n_node * REALNET_REWARD_NORM))
            reward = np.array(new_reward)
        return state, reward, done, global_reward

    def update_fingerprint(self, policy):
        for node_name, pi in zip(self.node_names, policy):
            self.nodes[node_name].fingerprint = np.array(pi)[:-1]
