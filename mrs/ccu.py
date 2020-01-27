import argparse
import logging.config

from fmlib.models.tasks import TaskPlan
from planner.planner import Planner
from ropod.structs.status import TaskStatus as TaskStatusConst
from ropod.utils.uuid import generate_uuid
from stn.exceptions.stp import NoSTPSolution

from mrs.allocation.auctioneer import Auctioneer
from mrs.config.configurator import Configurator
from mrs.db.models.actions import GoTo
from mrs.db.models.task import InterTimepointConstraint
from mrs.db.models.task import Task
from mrs.dispatching.dispatcher import Dispatcher
from mrs.execution.delay_recovery import DelayRecovery
from mrs.messages.assignment_update import AssignmentUpdate
from mrs.messages.dispatch_queue_update import DispatchQueueUpdate
from mrs.messages.task_status import TaskStatus
from mrs.simulation.simulator import Simulator, SimulatorInterface
from mrs.timetable.timetable_manager import TimetableManager


class CCU:

    _component_modules = {'simulator': Simulator,
                          'timetable_manager': TimetableManager,
                          'auctioneer': Auctioneer,
                          'dispatcher': Dispatcher,
                          'planner': Planner,
                          'delay_recovery': DelayRecovery}

    def __init__(self, config_file=None):
        self.logger = logging.getLogger("mrs.ccu")
        self.logger.info("Configuring CCU...")

        self.components = self.get_components(config_file)

        self.auctioneer = self.components.get('auctioneer')
        self.dispatcher = self.components.get('dispatcher')
        self.planner = self.components.get('planner')
        self.timetable_manager = self.components.get('timetable_manager')
        self.recovery_method = self.components.get("delay_recovery").method
        self.simulator_interface = SimulatorInterface(self.components.get('simulator'))

        self.api = self.components.get('api')
        self.ccu_store = self.components.get('ccu_store')

        self.api.register_callbacks(self)
        self.logger.info("Initialized CCU")

        self.task_plans = dict()

    def get_components(self, config_file):
        config = Configurator(config_file, component_modules=self._component_modules)
        return config.config_ccu()

    def start_test_cb(self, msg):
        self.logger.debug("Start test msg received")
        tasks = Task.get_tasks_by_status(TaskStatusConst.UNALLOCATED)
        for task in tasks:
            self.task_plans[task.task_id] = self.get_task_plan(task)
        self.auctioneer.allocate(tasks)

    def get_task_plan(self, task):
        path = self.planner.get_path(task.request.pickup_location, task.request.delivery_location)

        mean, variance = self.get_plan_work_time(path)
        work_time = InterTimepointConstraint(name="work_time", mean=mean, variance=variance)
        task.update_inter_timepoint_constraint(work_time.name, work_time.mean, work_time.variance)

        task_plan = TaskPlan()
        action = GoTo(action_id=generate_uuid(),
                      type="PICKUP-TO-DELIVERY",
                      locations=path,
                      estimated_duration=work_time)
        task_plan.actions.append(action)

        return task_plan

    def get_plan_work_time(self, plan):
        mean, variance = self.planner.get_estimated_duration(plan)
        return mean, variance

    def process_allocation(self):
        while self.auctioneer.allocations and self.auctioneer.round.finished:
            task_id, robot_ids = self.auctioneer.allocations.pop(0)
            self.logger.critical("Processing allocation of task: %s", task_id)
            task = self.auctioneer.allocated_tasks.get(task_id)
            task.assign_robots(robot_ids)
            self.update_task_plan(robot_ids)

            for robot_id in robot_ids:
                # TODO: Send g_graph only if it is diff from previous version
                self.send_d_graph_update(robot_id)

    def update_task_plan(self, robot_ids):
        for pre_task_action in self.auctioneer.pre_task_actions:
            task = Task.get_task(pre_task_action.task_id)
            task_plan = self.task_plans[task.task_id]
            if [action for action in task_plan.actions if action.type == "ROBOT-TO-PICKUP"]:
                task_plan.actions[0] = pre_task_action
            else:
                task_plan.actions.insert(0, pre_task_action)

            task.update_plan(robot_ids, task_plan)
            self.logger.debug('Task plan of task %s updated', task.task_id)
        self.auctioneer.pre_task_actions = list()

    def send_d_graph_update(self, robot_id):
        timetable = self.timetable_manager.get_timetable(robot_id)
        self.logger.critical("Sending DGraphUpdate to %s", robot_id)
        sub_stn = timetable.stn.get_subgraph(DispatchQueueUpdate.n_tasks)
        sub_dispatchable_graph = timetable.dispatchable_graph.get_subgraph(DispatchQueueUpdate.n_tasks)
        self.logger.debug("Sub stn: %s", sub_stn)
        self.logger.debug("Sub dispatchable graph: %s", sub_dispatchable_graph)
        dispatch_queue_update = DispatchQueueUpdate(self.timetable_manager.ztp,
                                                    sub_stn,
                                                    sub_dispatchable_graph)
        msg = self.api.create_message(dispatch_queue_update)
        self.api.publish(msg, peer=robot_id)

    def task_status_cb(self, msg):
        payload = msg['payload']
        task_status = TaskStatus.from_payload(payload)
        self.logger.debug("Received task status % for task %s ", task_status.status, task_status.task_id)
        task = Task.get_task(task_status.task_id)

        if task_status.status in [TaskStatusConst.COMPLETED, TaskStatusConst.CANCELED, TaskStatusConst.ABORTED]:
            self.auctioneer.remove_task(task)
            task.update_status(task_status.status)
            self.send_d_graph_update(task_status.robot_id)

        elif task_status.status == TaskStatusConst.UNALLOCATED:
            self.re_allocate(task)

        else:
            task.update_status(task_status.status)

    def re_allocate(self, task):
        self.logger.warning("Re-allocating task %s", task.task_id)
        self.auctioneer.remove_task(task)
        self.send_d_graph_update(task.assigned_robots[0])
        task.update_status(TaskStatusConst.UNALLOCATED)
        self.auctioneer.allocate(task)

    def assignment_update_cb(self, msg):
        payload = msg['payload']
        assignment_update = AssignmentUpdate.from_payload(payload)
        self.logger.debug("Assignment Update received")
        timetable = self.timetable_manager.get_timetable(assignment_update.robot_id)
        stn = timetable.stn

        for a in assignment_update.assignments:
            stn.assign_timepoint(a.assigned_time, a.task_id, a.node_type, force=True)
            stn.execute_timepoint(a.task_id, a.node_type)
            stn.execute_incoming_edge(a.task_id, a.node_type)
            stn.remove_old_timepoints()

        last_assignment = assignment_update.assignments.pop()
        last_executed_task = Task.get_task(last_assignment.task_id)

        self.logger.debug("Updated STN: %s", stn)
        timetable.stn = stn
        timetable.store()

        try:
            dispatchable_graph = timetable.compute_dispatchable_graph(stn)
            self.logger.debug("Updated DispatchableGraph %s: ", dispatchable_graph)
            timetable.dispatchable_graph = dispatchable_graph
            timetable.store()
            self.send_d_graph_update(assignment_update.robot_id)
        except NoSTPSolution:
            self.logger.warning("Temporal network becomes inconsistent")
            next_task = timetable.get_next_task(last_executed_task)
            if next_task:
                self.recover(next_task)
            else:
                print("dispatchable graph: ", timetable.dispatchable_graph)
                self.send_d_graph_update(assignment_update.robot_id)

    def recover(self, task):
        if self.recovery_method.name.endswith("abort"):
            self.logger.warning("Aborting allocation of task %s", task.task_id)
            task.update_status(TaskStatusConst.ABORTED)
            self.auctioneer.remove_task(task)
            self.send_d_graph_update(task.assigned_robots[0])
            self.auctioneer.tasks_to_allocate.pop(task.task_id)

        elif self.recovery_method.name.endswith("re-allocate"):
            self.re_allocate(task)

    def run(self):
        try:
            self.api.start()

            while True:
                self.simulator_interface.run()
                self.auctioneer.run()
                self.dispatcher.run()
                self.process_allocation()
                self.api.run()
        except (KeyboardInterrupt, SystemExit):
            self.api.shutdown()
            self.logger.info('FMS is shutting down')

    def shutdown(self):
        self.api.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', type=str, action='store', help='Path to the config file')

    args = parser.parse_args()
    ccu = CCU(args.file)
    ccu.run()


