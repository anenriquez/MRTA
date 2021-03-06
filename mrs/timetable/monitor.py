import logging
import time

from fmlib.models.tasks import TransportationTask as Task
from mrs.exceptions.allocation import TaskNotFound
from mrs.messages.remove_task import RemoveTaskFromSchedule
from mrs.messages.task_status import TaskStatus
from mrs.simulation.simulator import SimulatorInterface
from mrs.utils.time import relative_to_ztp
from ropod.structs.status import TaskStatus as TaskStatusConst, ActionStatus as ActionStatusConst
from ropod.utils.timestamp import TimeStamp
from stn.exceptions.stp import NoSTPSolution
from mrs.messages.task_status import ActionProgress


class TimetableMonitor(SimulatorInterface):
    def __init__(self, auctioneer, dispatcher, delay_recovery, **kwargs):
        simulator = kwargs.get('simulator')
        super().__init__(simulator)

        self.auctioneer = auctioneer
        self.dispatcher = dispatcher
        self.timetable_manager = auctioneer.timetable_manager
        self.recovery_method = delay_recovery
        self.d_graph_watchdog = kwargs.get("d_graph_watchdog", False)
        self.api = kwargs.get('api')

        self.tasks_to_remove = list()
        self.tasks_to_reallocate = list()
        self.completed_tasks = list()
        self.deleting_task = False
        self.processing_task = False
        self.logger = logging.getLogger("mrs.timetable.monitor")
        self.logger.critical("watchdog: %s", self.d_graph_watchdog)
        self.action_progress = dict()

    def configure(self, **kwargs):
        for key, value in kwargs.items():
            self.logger.debug("Adding %s", key)
            self.__dict__[key] = value

    def task_status_cb(self, msg):
        while self.deleting_task and not self.auctioneer.allocating_task:
            time.sleep(0.1)

        self.processing_task = True

        payload = msg['payload']
        timestamp = TimeStamp.from_str(msg["header"]["timestamp"]).to_datetime()
        task_status = TaskStatus.from_payload(payload)
        task_progress = task_status.task_progress

        task = Task.get_task(task_status.task_id)

        self.logger.debug("Received task status %s for task %s by %s", task_status.task_status, task_status.task_id,
                          task_status.robot_id)

        if task_status.task_status == TaskStatusConst.ONGOING:
            self._update_progress(task, task_progress, timestamp)
            self._update_timetable(task, task_status.robot_id, task_progress, timestamp)
            self._update_task_schedule(task, task_progress, timestamp)
            task.update_status(task_status.task_status)

        elif task_status.task_status == TaskStatusConst.COMPLETED:
            self.logger.debug("Adding task %s to tasks to remove", task.task_id)
            self.tasks_to_remove.append((task, task_status.task_status))

        elif task_status.task_status == TaskStatusConst.UNALLOCATED:
            self._re_allocate(task)

        elif task_status.task_status == TaskStatusConst.PREEMPTED:
            if task.status.status == TaskStatusConst.PREEMPTED:
                self.logger.warning("Task %s is already preempted", task_status.task_id)
                return
            try:
                self._remove_task(task, task_status.task_status)
            except TaskNotFound:
                return

        self.processing_task = False

    def _update_progress(self, task, task_progress, timestamp):
        self.logger.debug("Updating progress of task %s action %s status %s", task.task_id, task_progress.action_id,
                          task_progress.action_status.status)

        updating_finish_time = False

        if task_progress.action_id not in self.action_progress:
            action_progress = ActionProgress(task_progress.action_id, timestamp)
            self.action_progress[task_progress.action_id] = action_progress
        else:
            updating_finish_time = True
            action_progress = self.action_progress.get(task_progress.action_id)
            action_progress.finish_time = timestamp

        self.logger.debug("Current action progress: start time %s, finish time %s", action_progress.start_time,
                          action_progress.finish_time)

        kwargs = {}
        if task_progress.action_status.status == ActionStatusConst.ONGOING:
            kwargs.update(start_time=timestamp)
        elif task_progress.action_status.status == ActionStatusConst.COMPLETED:
            kwargs.update(start_time=action_progress.start_time, finish_time=timestamp)

        self.logger.debug("Updating action progress with %s: ", kwargs)

        task.update_progress(task_progress.action_id, task_progress.action_status.status, **kwargs)
        updated_action_progress = task.status.progress.get_action(task_progress.action_id)

        if updated_action_progress.start_time is None or (updating_finish_time and action_progress.finish_time is None):
            self.logger.warning("Action progress was not updated. Trying again..")
            task.update_progress(task_progress.action_id, task_progress.action_status.status, **kwargs)
            updated_action_progress = task.status.progress.get_action(task_progress.action_id)

        self.logger.debug("Updated action progress: status %s, start time %s, finish time %s", updated_action_progress.status,
                          updated_action_progress.start_time, updated_action_progress.finish_time)

    def _update_timetable(self, task, robot_id, task_progress, timestamp):
        timetable = self.timetable_manager.get_timetable(robot_id)
        self.logger.debug("Updating timetable of robot: %s", robot_id)

        first_action_id = task.plan[0].actions[0].action_id

        if task_progress.action_id == first_action_id and \
                task_progress.action_status.status == ActionStatusConst.ONGOING:
            node_id, node = timetable.stn.get_node_by_type(task.task_id, 'start')
            self._update_timepoint(task, timetable, timestamp, node_id)
            self.performance_tracker.update_scheduling_metrics(task.task_id, timetable)
        else:
            # An action could be associated to two nodes, e.g., between pickup and delivery there is only one action
            nodes = timetable.stn.get_nodes_by_action(task_progress.action_id)

            for node_id, node in nodes:
                if (node.node_type == 'pickup' and
                    task_progress.action_status.status == ActionStatusConst.ONGOING) or \
                        (node.node_type == 'delivery' and
                         task_progress.action_status.status == ActionStatusConst.COMPLETED):
                    self._update_timepoint(task, timetable, timestamp, node_id)

    def _update_timepoint(self, task, timetable, assigned_time, node_id):
        r_assigned_time = relative_to_ztp(timetable.ztp, assigned_time)
        timetable.check_is_task_delayed(task, r_assigned_time, node_id)
        self.performance_tracker.update_delay(task.task_id, r_assigned_time, node_id, timetable)
        self.performance_tracker.update_earliness(task.task_id, r_assigned_time, node_id, timetable)
        timetable.update_timepoint(r_assigned_time, node_id)

        nodes = timetable.stn.get_nodes_by_task(task.task_id)
        self._update_edge(timetable, 'start', 'pickup', nodes)
        self._update_edge(timetable, 'pickup', 'delivery', nodes)
        self.performance_tracker.update_timetables(timetable)
        self.auctioneer.changed_timetable.append(timetable.robot_id)

        self.logger.debug("Updated stn: \n %s ", timetable.stn)
        self.logger.debug("Updated dispatchable graph: \n %s", timetable.dispatchable_graph)
        timetable.store()

        if self.d_graph_watchdog:
            next_task = timetable.get_next_task(task)
            self._re_compute_dispatchable_graph(timetable, next_task)

    @staticmethod
    def _update_edge(timetable, start_node, finish_node, nodes):
        node_ids = [node_id for node_id, node in nodes if (node.node_type == start_node and node.is_executed) or
                    (node.node_type == finish_node and node.is_executed)]
        if len(node_ids) == 2:
            timetable.execute_edge(node_ids[0], node_ids[1])

    def _update_task_schedule(self, task, task_progress, timestamp):
        # TODO: Get schedule from dispatchable graph
        first_action_id = task.plan[0].actions[0].action_id
        last_action_id = task.plan[0].actions[-1].action_id

        if task_progress.action_id == first_action_id and \
                task_progress.action_status.status == ActionStatusConst.ONGOING:
            self.logger.debug("Task %s start time %s", task.task_id, timestamp)
            task_schedule = {"start_time": timestamp,
                             "finish_time": task.finish_time}
            task.update_schedule(task_schedule)

        elif task_progress.action_id == last_action_id and \
                task_progress.action_status.status == ActionStatusConst.COMPLETED:
            self.logger.debug("Task %s finish time %s", task.task_id, timestamp)
            task_schedule = {"start_time": task.start_time,
                             "finish_time": timestamp}
            task.update_schedule(task_schedule)

    def _re_allocate(self, task):
        self.logger.critical("Re-allocating task %s", task.task_id)
        if task.status.status == TaskStatusConst.UNALLOCATED:
            self.logger.warning("Task %s is already unallocated", task.task_id)
            return
        try:
            self._remove_task(task, TaskStatusConst.UNALLOCATED)
        except TaskNotFound:
            return
        task.unassign_robots()
        self.auctioneer.allocated_tasks.pop(task.task_id)
        self.auctioneer.allocate(task)
        self.tasks_to_reallocate.append(task)

    def _re_compute_dispatchable_graph(self, timetable, next_task=None):
        if timetable.stn.is_empty():
            self.logger.warning("Timetable of %s is empty", timetable.robot_id)
            return
        self.logger.critical("Recomputing dispatchable graph of robot %s", timetable.robot_id)
        try:
            start = time.time()
            timetable.dispatchable_graph = timetable.compute_dispatchable_graph(timetable.stn)
            end = time.time()
            self.logger.debug("Dispatchable graph robot %s: %s", timetable.robot_id, timetable.dispatchable_graph)
            self.performance_tracker.update_timetables(timetable)
            self.performance_tracker.update_dgraph_recomputation_time(timetable.robot_id, end-start)
            self.dispatcher.send_d_graph_update(timetable.robot_id)
            timetable.store()
        except NoSTPSolution:
            self.logger.warning("Temporal network is inconsistent")
            self.logger.debug("STN robot %s: %s", timetable.robot_id, timetable.stn)
            self.logger.debug("Dispatchable graph robot %s: %s", timetable.robot_id, timetable.dispatchable_graph)
            if next_task:
                self.recover(next_task)

    def recover(self, task):
        if self.recovery_method.name == "preempt":
            self._remove_task(task, TaskStatusConst.PREEMPTED)
        elif self.recovery_method.name == "re-allocate":
            self._re_allocate(task)

    def _remove_task(self, task, status):
        self.logger.critical("Deleting task %s from timetable and changing its status to %s", task.task_id, status)
        for robot_id in task.assigned_robots:
            timetable = self.timetable_manager.get_timetable(robot_id)
            self.auctioneer.changed_timetable.append(timetable.robot_id)

            if not timetable.has_task(task.task_id):
                self.logger.warning("Robot %s does not have task %s in its timetable: ", robot_id, task.task_id)
                raise TaskNotFound

            prev_task = timetable.get_previous_task(task)
            next_task = timetable.get_next_task(task)

            earliest_task = timetable.get_task(position=1)

            if earliest_task and task.task_id == earliest_task.task_id and next_task:
                self._remove_first_task(task, next_task, status, timetable)
            else:
                timetable.remove_task(task.task_id)

            if status == TaskStatusConst.COMPLETED:
                self.update_robot_poses(task)

            if prev_task and next_task:
                self.update_pre_task_constraint(prev_task, next_task, timetable)

            self.logger.debug("STN robot %s: %s", robot_id, timetable.stn)
            self.logger.debug("Dispatchable graph robot %s: %s", robot_id, timetable.dispatchable_graph)
            timetable.store()

            task.update_status(status)
            self.send_remove_task(task.task_id, status, robot_id)
            self._re_compute_dispatchable_graph(timetable, next_task)

    @staticmethod
    def _remove_first_task(task, next_task, status, timetable):
        if status == TaskStatusConst.COMPLETED:
            earliest_time = timetable.stn.get_time(task.task_id, 'delivery', False)
            timetable.stn.assign_earliest_time(earliest_time, next_task.task_id, 'start', force=True)
        else:
            nodes = timetable.stn.get_nodes_by_task(task.task_id)
            node_id, node = nodes[0]
            earliest_time = timetable.stn.get_node_earliest_time(node_id)
            timetable.stn.assign_earliest_time(earliest_time, next_task.task_id, 'start', force=True)

        start_next_task = timetable.dispatchable_graph.get_time(next_task.task_id, 'start')
        if start_next_task < earliest_time:
            timetable.dispatchable_graph.assign_earliest_time(earliest_time, next_task.task_id, 'start', force=True)

        timetable.remove_task(task.task_id)

    def update_pre_task_constraint(self, prev_task, task, timetable):
        self.logger.critical("Update pre_task constraint of task %s", task.task_id)
        prev_location = prev_task.request.delivery_location
        path = self.dispatcher.planner.get_path(prev_location, task.request.pickup_location)
        mean, variance = self.dispatcher.planner.get_estimated_duration(path)

        stn_task = timetable.get_stn_task(task.task_id)
        stn_task.update_edge("travel_time", mean, variance)
        timetable.add_stn_task(stn_task)
        timetable.update_task(stn_task)

    def validate_next_tasks(self, timetable, task_to_delete):
        # Get next task until the next task is valid
        tasks_to_recover = list()
        next_task = timetable.get_next_task(task_to_delete)
        if next_task:
            tasks_to_recover = self.check_next_task_validity(timetable, task_to_delete, next_task, tasks_to_recover)
            for task_to_recover in tasks_to_recover:
                self.recover(task_to_recover)

    def check_next_task_validity(self, timetable, task, next_task, tasks_to_recover):
        if timetable.is_next_task_invalid(task, next_task):
            tasks_to_recover.append(next_task)
            next_task = timetable.get_next_task(next_task)
            if next_task:
                self.check_next_task_validity(timetable, task, next_task, tasks_to_recover)
        return tasks_to_recover

    def send_remove_task(self, task_id, status, robot_id):
        remove_task = RemoveTaskFromSchedule(task_id, status)
        msg = self.api.create_message(remove_task)
        self.api.publish(msg, peer=robot_id + '_proxy')

    def update_robot_poses(self, task):
        for robot_id in task.assigned_robots:
            x, y, theta = self.dispatcher.planner.get_pose(task.request.delivery_location)
            self.dispatcher.fleet_monitor.update_robot_pose(robot_id, x=x, y=y, theta=theta)

    def run(self):
        # TODO: Check how this works outside simulation
        ready_to_be_removed = list()
        if not self.processing_task and not self.auctioneer.allocating_task:

            for task, status in self.tasks_to_remove:
                if task.finish_time < self.get_current_time():
                    self.deleting_task = True
                    ready_to_be_removed.append((task, status))

            for task, status in ready_to_be_removed:
                self.tasks_to_remove.remove((task, status))
                if status == TaskStatusConst.COMPLETED:
                    self.completed_tasks.append(task)
                self._remove_task(task, status)

        if self.deleting_task and not self.completed_tasks:
            self.deleting_task = False
