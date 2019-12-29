import logging
from datetime import timedelta

from ropod.structs.task import TaskStatus as TaskStatusConst
from ropod.utils.timestamp import TimeStamp

from mrs.allocation.round import Round
from mrs.db.models.task import Task
from mrs.exceptions.allocation import AlternativeTimeSlot
from mrs.exceptions.allocation import InvalidAllocation
from mrs.exceptions.allocation import NoAllocation
from mrs.messages.bid import Bid, NoBid, SoftBid
from mrs.messages.task_announcement import TaskAnnouncement
from mrs.messages.task_contract import TaskContract, TaskContractAcknowledgment
from mrs.utils.utils import is_valid_time

""" Implements a variation of the the TeSSI algorithm using the bidding_rule 
specified in the config file
"""


class Auctioneer(object):

    def __init__(self, stp_solver, timetable_manager, closure_window=5, freeze_window=300, **kwargs):

        self.logger = logging.getLogger("mrs.auctioneer")
        self.api = kwargs.get('api')
        self.ccu_store = kwargs.get('ccu_store')
        self.robot_ids = list()
        self.timetable_manager = timetable_manager

        self.stp_solver = stp_solver

        self.closure_window = timedelta(seconds=closure_window)
        self.freeze_window = timedelta(seconds=freeze_window)
        self.alternative_timeslots = kwargs.get('alternative_timeslots', False)

        self.logger.debug("Auctioneer started")

        self.tasks_to_allocate = dict()
        self.allocations = list()
        self.pre_task_actions = dict()
        self.winning_bid = None
        self.waiting_for_user_confirmation = list()
        self.round = Round(self.robot_ids)

    def configure(self, **kwargs):
        api = kwargs.get('api')
        ccu_store = kwargs.get('ccu_store')
        if api:
            self.api = api
        if ccu_store:
            self.ccu_store = ccu_store

    def register_robot(self, robot_id):
        self.logger.debug("Registering robot %s", robot_id)
        self.robot_ids.append(robot_id)
        self.timetable_manager.register_robot(robot_id)

    def set_zero_timepoint(self, time_):
        self.timetable_manager.zero_timepoint = time_

    def update_tasks_to_allocate(self):
        tasks = Task.get_tasks_by_status(TaskStatusConst.UNALLOCATED)
        self.tasks_to_allocate = dict()
        for task in tasks:
            self.tasks_to_allocate[task.task_id] = task

    def run(self):
        if self.tasks_to_allocate and self.round.finished:
            self.announce_task()

        if self.round.opened and self.round.time_to_close():
            try:
                round_result = self.round.get_result()
                self.process_round_result(round_result)

            except NoAllocation as exception:
                self.logger.warning("No allocation made in round %s ", exception.round_id)
                self.update_tasks_to_allocate()
                self.round.finish()

            except AlternativeTimeSlot as exception:
                self.process_alternative_timeslot(exception)

    def process_round_result(self, round_result):
        bid, time_to_allocate = round_result
        self.winning_bid = bid
        self.announce_winner(bid.task_id, bid.robot_id)

    def process_alternative_timeslot(self, exception):
        bid = exception.bid
        time_to_allocate = exception.time_to_allocate
        alternative_start_time = bid.alternative_start_time
        alternative_allocation = (bid.task_id, [bid.robot_id], alternative_start_time)

        self.logger.debug("Alternative timeslot for task %s: robot %s, alternative start time: %s ", bid.task_id,
                          bid.robot_id, bid.alternative_start_time)

        self.waiting_for_user_confirmation.append(alternative_allocation)

        # TODO: Prompt the user to accept the alternative timeslot
        # For now, accept always
        self.winning_bid = bid
        self.announce_winner(bid.task_id, bid.robot_id)

    def process_allocation(self):
        task = self.tasks_to_allocate.pop(self.winning_bid.task_id)

        try:
            travel_time = self.winning_bid.pre_task_action.estimated_duration
            task.update_inter_timepoint_constraint(**travel_time.to_dict())
            self.pre_task_actions[self.winning_bid.task_id] = self.winning_bid.pre_task_action

            self.timetable_manager.update_timetable(self.winning_bid.robot_id,
                                                    self.winning_bid.insertion_point,
                                                    self.winning_bid.metrics.temporal, task)

            allocation = (self.winning_bid.task_id, [self.winning_bid.robot_id])
            self.logger.debug("Allocation: %s", allocation)
            self.logger.debug("Tasks to allocate %s", [task_id for task_id, task in self.tasks_to_allocate.items()])

            self.logger.debug("Updating task status to ALLOCATED")
            task.update_status(TaskStatusConst.ALLOCATED)

            self.allocations.append(allocation)

        except InvalidAllocation as e:
            self.logger.warning("The allocation of task %s to robot %s is inconsistent. Aborting allocation."
                                "Task %s will be included in next allocation round", e.task_id, e.robot_id, e.task_id)
            self.tasks_to_allocate[task.task_id] = task
            self.round.finish()

    def allocate(self, tasks):
        tasks_to_allocate = dict()
        if isinstance(tasks, list):
            self.logger.debug('Auctioneer received a list of tasks')
            for task in tasks:
                if not isinstance(task, Task):
                    task = Task.from_task(task)
                tasks_to_allocate[task.task_id] = task
        else:
            self.logger.debug('Auctioneer received one task')
            if not isinstance(tasks, Task):
                tasks = Task.from_task(tasks)
            tasks_to_allocate[tasks.task_id] = tasks
        self.tasks_to_allocate = tasks_to_allocate

    def announce_task(self):
        self.timetable_manager.fetch_timetables()
        self.update_tasks_to_allocate()
        if not self.tasks_to_allocate:
            return

        tasks = list(self.tasks_to_allocate.values())
        earliest_task = Task.get_earliest_task(tasks)
        closure_time = earliest_task.get_timepoint_constraint("pickup").earliest_time - self.closure_window
        if not is_valid_time(closure_time) and not self.alternative_timeslots:
            self.logger.warning("Task %s cannot not be allocated at it's given temporal constraints", earliest_task.task_id)
            earliest_task.remove()
            return

        self.round = Round(self.robot_ids,
                           n_tasks=len(tasks),
                           closure_time=closure_time,
                           alternative_timeslots=self.alternative_timeslots)

        task_announcement = TaskAnnouncement(tasks, self.round.id, self.timetable_manager.zero_timepoint)

        self.logger.debug("Starting round: %s", self.round.id)
        self.logger.debug("Number of tasks to allocate: %s", len(tasks))

        msg = self.api.create_message(task_announcement)

        self.logger.debug("Auctioneer announces tasks %s", [task_id for task_id, task in self.tasks_to_allocate.items()])

        self.round.start()
        self.api.publish(msg, groups=['TASK-ALLOCATION'])

    def bid_cb(self, msg):
        payload = msg['payload']
        self.round.process_bid(payload, Bid)

    def no_bid_cb(self, msg):
        payload = msg['payload']
        self.round.process_bid(payload, NoBid)

    def soft_bid_cb(self, msg):
        payload = msg['payload']
        self.round.process_bid(payload, SoftBid)

    def task_contract_acknowledgement_cb(self, msg):
        payload = msg['payload']
        task_contract_acknowledgement = TaskContractAcknowledgment.from_payload(payload)
        n_tasks_before = len(self.timetable_manager.get_timetable(task_contract_acknowledgement.robot_id).get_tasks())

        if task_contract_acknowledgement.accept and \
                TaskContract.is_valid(n_tasks_before, task_contract_acknowledgement.n_tasks):
            self.logger.debug("Concluding allocation of task %s", task_contract_acknowledgement.task_id)
            self.process_allocation()
        else:
            self.logger.warning("Round %s has to be repeated", self.round.id)
        self.round.finish()

    def announce_winner(self, task_id, robot_id):
        task_contract = TaskContract(task_id, robot_id)
        msg = self.api.create_message(task_contract)
        self.api.publish(msg, groups=['TASK-ALLOCATION'])

    def archive_task(self, archive_task):
        self.logger.debug("Deleting task %s", archive_task.task_id)
        timetable = self.timetable_manager.get_timetable(archive_task.robot_id)
        timetable.remove_task(archive_task.node_id)
        task = Task.get_task(archive_task.task_id)
        task.update_status(TaskStatusConst.COMPLETED)
        self.logger.debug("STN robot %s: %s", archive_task.robot_id, timetable.stn)
        self.logger.debug("Dispatchable graph robot %s: %s", archive_task.robot_id, timetable.dispatchable_graph)

    def get_task_schedule(self, task_id, robot_id):
        # For now, returning the start navigation time from the dispatchable graph
        task_schedule = dict()

        timetable = self.timetable_manager.timetables.get(robot_id)

        relative_start_time = timetable.dispatchable_graph.get_time(task_id, "start")
        relative_pickup_time = timetable.dispatchable_graph.get_time(task_id, "pickup")
        relative_latest_delivery_time = timetable.dispatchable_graph.get_time(task_id, "delivery", False)

        self.logger.debug("Current time %s: ", TimeStamp())
        self.logger.debug("zero_timepoint %s: ", self.timetable_manager.zero_timepoint)
        self.logger.debug("Relative start navigation time: %s", relative_start_time)
        self.logger.debug("Relative pickup time: %s", relative_pickup_time)
        self.logger.debug("Relative latest delivery time: %s", relative_latest_delivery_time)

        start_time = self.timetable_manager.zero_timepoint + timedelta(seconds=relative_start_time)
        pickup_time = self.timetable_manager.zero_timepoint + timedelta(seconds=relative_pickup_time)
        delivery_time = self.timetable_manager.zero_timepoint + timedelta(seconds=relative_latest_delivery_time)

        self.logger.debug("Task %s start time: %s", task_id, start_time)
        self.logger.debug("Task %s pickup time : %s", task_id, pickup_time)
        self.logger.debug("Task %s latest delivery time: %s", task_id, delivery_time)

        task_schedule['start_time'] = start_time.to_datetime()
        task_schedule['finish_time'] = delivery_time.to_datetime()

        return task_schedule

