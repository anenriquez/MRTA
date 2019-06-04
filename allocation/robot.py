import copy
import uuid
import time
import numpy as np
import os
import argparse
import logging
import logging.config
import yaml
from ropod.pyre_communicator.base_class import RopodPyre
from allocation.config.config_file_reader import ConfigFileReader
from temporal.structs.task import Task
from temporal.networks.stn import STN
from temporal.networks.stn import Scheduler
from temporal.networks.pstn import PSTN
from temporal.networks.pstn import SchedulerPSTN


'''  Implements the TeSSI algorithm with different bidding rules:

    - Rule 1: Lowest completion time (last task finish time - first task start time)
    - Rule 2: Lowest combination of completion time and travel distance_robot
    - Rule 3: Lowest makespan (finish time of the last task in the schedule)
    - Rule 4: Lowest combination of makespan and travel distance_robot
    - Rule 5: Lowest idle time of the robot with more tasks
'''


class Robot(RopodPyre):
    #  Bidding rules
    COMPLETION_TIME = 1
    COMPLETION_TIME_DISTANCE = 2
    MAKESPAN = 3
    MAKESPAN_DISTANCE = 4
    IDLE_TIME = 5
    MAX_SEED = 2 ** 31 - 1

    def __init__(self, robot_id, config_params):
        self.id = robot_id
        self.bidding_rule = config_params.bidding_rule
        self.zyre_params = config_params.task_allocator_zyre_params

        super().__init__(self.id, self.zyre_params.groups, self.zyre_params.message_types, acknowledge=False)

        self.logger = logging.getLogger('robot: %s' % robot_id)
        self.logger.debug("This is a debug message")

        type_temporal_network = config_params.type_temporal_network

        if type_temporal_network == 'stn':
            self.temporal_network = STN()
            self.scheduler = Scheduler()
        else:
            self.temporal_network = PSTN()
            random_seed = np.random.randint(MAX_SEED)
            seed_gen = np.random.RandomState(random_seed)
            seed = seed_gen.randint(MAX_SEED)
            self.scheduler = SchedulerPSTN(seed)

        self.dataset_start_time = 0
        self.scheduled_tasks = list()
        self.idle_time = 0.
        self.distance = 0.

        self.bid_round = 0.
        self.scheduled_tasks_round = list()

        # Weighting factor used for the dual bidding rule
        self.alpha = 0.1

    def reinitialize_auction_variables(self):
        self.bid_round = None
        self.scheduled_tasks_round = list()

    def receive_msg_cb(self, msg_content):
        dict_msg = self.convert_zyre_msg_to_dict(msg_content)
        if dict_msg is None:
            return
        message_type = dict_msg['header']['type']

        if message_type == 'START':
            self.dataset_start_time = dict_msg['payload']['start_time']
            self.logger.debug("Received dataset start time %s", self.dataset_start_time)

        elif message_type == 'TASK-ANNOUNCEMENT':
            self.reinitialize_auction_variables()
            n_round = dict_msg['payload']['round']
            tasks = dict_msg['payload']['tasks']
            self.compute_bids(tasks, n_round)

        elif message_type == "ALLOCATION":
            task_id = dict_msg['payload']['task_id']
            winner_id = dict_msg['payload']['winner_id']
            if winner_id == self.id:
                self.allocate_to_robot(task_id)

    def compute_bids(self, tasks, n_round):
        bids = dict()
        empty_bids = list()

        for task_id, task_info in tasks.items():
            task = Task.from_dict(task_info)
            self.logger.debug("Computing bid of task %s", task.id)
            # Insert task in each possible position of the stnu
            best_bid, best_schedule = self.insert_task(task)
            # self.insert_task(task)

            if best_bid != np.inf:
                bids[task_id] = dict()
                bids[task_id]['bid'] = best_bid
                bids[task_id]['scheduled_tasks'] = best_schedule
            else:
                empty_bids.append(task_id)

        if bids:
            # Send the smallest bid
            task_id_bid, smallest_bid = self.get_smallest_bid(bids, n_round)
            self.send_bid(n_round, task_id_bid, smallest_bid)
        else:
            # Send an empty bid with task ids of tasks that could not be allocated
            self.send_empty_bid(n_round, empty_bids)

    def insert_task(self, task):
        best_bid = float('Inf')
        best_schedule = list()

        n_scheduled_tasks = len(self.scheduled_tasks)

        for i in range(0, n_scheduled_tasks + 1):
            self.scheduled_tasks.insert(i, task)
            # TODO check if the robot can make it to the first task in the schedule, if not, return
            self.temporal_network.build_temporal_network(self.scheduled_tasks)

            print(self.temporal_network)
            print(self.temporal_network.nodes.data())
            print(self.temporal_network.edges.data())

            minimal_network = self.temporal_network.floyd_warshall()
            if self.temporal_network.is_consistent(minimal_network):
                self.temporal_network.update_edges(minimal_network)
                alpha, schedule = self.scheduler.get_schedule(self.temporal_network, "earliest")

                bid = self.compute_bid(schedule)
                if bid < best_bid:
                    best_bid = bid
                    best_schedule = copy.deepcopy(self.scheduled_tasks)

            # Restore new_schedule for the next iteration
            self.scheduled_tasks.pop(i)

        return best_bid, best_schedule

    # def update_temporal_network(self, minimal_network):
    #     self.temporal_network.update_edges(minimal_network)
    #     self.temporal_network.update_time_schedule(minimal_network)

    def compute_bid(self, schedule):
        if self.bidding_rule == self.COMPLETION_TIME:
            bid = self.scheduler.get_completion_time(schedule)
            print("Completion time: ", bid)

        # elif self.bidding_rule == self.COMPLETION_TIME_DISTANCE:
        #     completion_time = self.temporal_network.get_completion_time()
        #     distance = self.compute_distance(self.scheduled_tasks)
        #     bid = (self.alpha * completion_time) + (1 - self.alpha) * (distance - self.distance)
        #
        # elif self.bidding_rule == self.MAKESPAN:
        #     bid = self.temporal_network.get_makespan()
        #
        # elif self.bidding_rule == self.MAKESPAN_DISTANCE:
        #     makespan = self.temporal_network.get_makespan()
        #     distance = self.compute_distance(self.scheduled_tasks)
        #     bid = (self.alpha * makespan) + (1 - self.alpha) * (distance - self.distance)
        #
        # elif self.bidding_rule == self.IDLE_TIME:
        #     bid = 0
            # TODO
        return bid

    def compute_distance(self, schedule):
        ''' Computes the travel cost (distance traveled) for performing all
        tasks in the schedule (list of tasks)
        '''
        # TODO
        distance = 0
        return distance

    def get_smallest_bid(self, bids, n_round):
        '''
        Get the smallest bid among all bids.
        Each robot submits only its smallest bid in each round
        If two or more tasks have the same bid, the robot bids for the task with the lowest task_id
        '''
        smallest_bid = dict()
        smallest_bid['bid'] = np.inf
        task_id_bid = None
        lowest_task_id = ''

        for task_id, bid_info in bids.items():
            if bid_info['bid'] < smallest_bid['bid']:
                smallest_bid = copy.deepcopy(bid_info)
                task_id_bid = task_id
                lowest_task_id = task_id_bid

            elif bid_info['bid'] == smallest_bid['bid'] and task_id < lowest_task_id:
                smallest_bid = copy.deepcopy(bid_info)
                task_id_bid = task_id
                lowest_task_id = task_id_bid

        if smallest_bid != np.inf:
            return task_id_bid, smallest_bid

    def send_bid(self, n_round, task_id, bid):
        ''' Create bid_msg and send it to the auctioneer '''
        bid_msg = dict()
        bid_msg['header'] = dict()
        bid_msg['payload'] = dict()
        bid_msg['header']['type'] = 'BID'
        bid_msg['header']['metamodel'] = 'ropod-msg-schema.json'
        bid_msg['header']['msgId'] = str(uuid.uuid4())
        bid_msg['header']['timestamp'] = int(round(time.time()) * 1000)

        bid_msg['payload']['metamodel'] = 'ropod-bid-schema.json'
        bid_msg['payload']['robot_id'] = self.id
        bid_msg['payload']['n_round'] = n_round
        bid_msg['payload']['task_id'] = task_id
        bid_msg['payload']['bid'] = bid['bid']

        self.bid_round = bid['bid']
        self.scheduled_tasks_round = bid['scheduled_tasks']

        tasks = [task.id for task in self.scheduled_tasks_round]

        self.logger.debug("Round %s: Robod_id %s bids %s for task %s and scheduled_tasks %s", n_round, self.id, self.bid_round, task_id, tasks)
        self.whisper(bid_msg, peer='auctioneer')

    def send_empty_bid(self, n_round, empty_bids):
        '''
        Create empty_bid_msg for each task in empty_bids and send it to the auctioneer
        '''
        empty_bid_msg = dict()
        empty_bid_msg['header'] = dict()
        empty_bid_msg['payload'] = dict()
        empty_bid_msg['header']['type'] = 'EMPTY-BID'
        empty_bid_msg['header']['metamodel'] = 'ropod-msg-schema.json'
        empty_bid_msg['header']['msgId'] = str(uuid.uuid4())
        empty_bid_msg['header']['timestamp'] = int(round(time.time()) * 1000)

        empty_bid_msg['payload']['metamodel'] = 'ropod-bid-schema.json'
        empty_bid_msg['payload']['robot_id'] = self.id
        empty_bid_msg['payload']['n_round'] = n_round
        empty_bid_msg['payload']['task_ids'] = list()

        for task_id in empty_bids:
            empty_bid_msg['payload']['task_ids'].append(task_id)

        self.logger.debug("Round %s: Robot id %s sends empty bid for tasks %s", n_round, self.id, empty_bids)
        self.whisper(empty_bid_msg, peer='auctioneer')

    def allocate_to_robot(self, task_id):
        # Update the scheduled tasks
        self.scheduled_tasks = copy.deepcopy(self.scheduled_tasks_round)

        self.logger.debug("Robot %s allocated task %s", self.id, task_id)
        tasks = [task.id for task in self.scheduled_tasks]
        self.logger.debug("Tasks scheduled to robot %s:%s", self.id, tasks)
        # Update travel distance and idle time
        self.distance = self.compute_distance(self.scheduled_tasks)
        if self.bidding_rule == self.IDLE_TIME:
            self.idle_time += self.bid_round

        self.send_schedule()

    def send_schedule(self):
        ''' Sends the updated schedule of the robot to the auctioneer.
        '''
        schedule_msg = dict()
        schedule_msg['header'] = dict()
        schedule_msg['payload'] = dict()
        schedule_msg['header']['type'] = 'SCHEDULE'
        schedule_msg['header']['metamodel'] = 'ropod-msg-schema.json'
        schedule_msg['header']['msgId'] = str(uuid.uuid4())
        schedule_msg['header']['timestamp'] = int(round(time.time()) * 1000)
        schedule_msg['payload']['metamodel'] = 'ropod-msg-schema.json'
        schedule_msg['payload']['robot_id'] = self.id
        schedule_msg['payload']['schedule'] = list()
        for i, task in enumerate(self.scheduled_tasks):
            schedule_msg['payload']['schedule'].append(task.to_dict())

        self.logger.debug("Robot sends its updated schedule to the auctioneer.")

        self.whisper(schedule_msg, peer='auctioneer')


if __name__ == '__main__':
    code_dir = os.path.abspath(os.path.dirname(__file__))
    main_dir = os.path.dirname(code_dir)

    config_params = ConfigFileReader.load("../config/config.yaml")

    parser = argparse.ArgumentParser()
    parser.add_argument('ropod_id', type=str, help='example: ropod_001')
    args = parser.parse_args()
    ropod_id = args.ropod_id

    with open('../config/logging.yaml', 'r') as f:
        config = yaml.safe_load(f.read())
        logging.config.dictConfig(config)


    # time.sleep(5)

    robot = Robot(ropod_id, config_params)
    robot.start()

    try:
        while True:
            time.sleep(0.5)
    except (KeyboardInterrupt, SystemExit):
        # logging.info("Terminating %s proxy ...", ropod_id)
        robot.shutdown()
        # logging.info("Exiting...")
        print("Exiting")
