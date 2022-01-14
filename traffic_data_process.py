import pickle

with open('./input data/data.pickle', 'rb') as f:
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
            self.Vissim.Net.VehicleRoutingDecisionsStatic.ItemByKey(svrd_id).VehRoutSta.ItemByKey(svr_id).SetAttValue(f'RelFlow({i})', trf / total)


# Set Vehicle Composition for each direction
with open('./input data/weight.pickle', 'rb') as f:
    df2 = pickle.load(f)

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

