from fmlib.db.mongo import MongoStore
from fmlib.models.actions import GoTo as Action
from fmlib.models.requests import TransportationRequest
from fmlib.models.tasks import TaskStatus
from fmlib.models.tasks import TransportationTask as Task
from pymodm import fields, MongoModel
from pymodm.context_managers import switch_collection
from pymodm.errors import DoesNotExist
from pymodm.manager import Manager
from pymodm.queryset import QuerySet

from mrs.db.models.bid import BidTime
from mrs.db.models.performance.robot import RobotPerformance
from mrs.db.models.performance.task import TaskPerformance
from mrs.db.models.round import Round


class ExperimentQuerySet(QuerySet):

    def by_dataset(self, dataset):
        return self.raw({"dataset": dataset})

    def by_bidding_rule(self, bidding_rule):
        return self.raw({'bidding_rule': bidding_rule})

    def by_dataset_and_bidding_rule(self, dataset, bidding_rule):
        return self.raw({"dataset": dataset}) and self.raw({"bidding_rule": bidding_rule})

    def by_run_id(self, run_id):
        return self.get({'_id': run_id})


ExperimentManager = Manager.from_queryset(ExperimentQuerySet)


class Experiment(MongoModel):
    run_id = fields.IntegerField(primary_key=True)
    name = fields.CharField()
    approach = fields.CharField()
    bidding_rule = fields.CharField()
    dataset = fields.CharField()
    requests = fields.EmbeddedDocumentListField(TransportationRequest)
    tasks = fields.EmbeddedDocumentListField(Task)
    actions = fields.EmbeddedDocumentListField(Action)
    tasks_status = fields.EmbeddedDocumentListField(TaskStatus)
    tasks_performance = fields.EmbeddedDocumentListField(TaskPerformance)
    robots_performance = fields.EmbeddedDocumentListField(RobotPerformance)
    rounds = fields.EmbeddedDocumentListField(Round)
    bid_times = fields.EmbeddedDocumentListField(BidTime, blank=True)

    objects = ExperimentManager()

    class Meta:
        ignore_unknown_fields = True

    @classmethod
    def create_new(cls, name, approach, bidding_rule, dataset, new_run=True):
        requests = cls.get_requests()
        tasks = cls.get_tasks()
        actions = cls.get_actions()
        tasks_status = cls.get_tasks_status(tasks)
        tasks_performance = cls.get_tasks_performance()
        robots_performance = cls.get_robots_performance()
        rounds = cls.get_rounds()
        bid_times = cls.get_bid_times(robots_performance)

        kwargs = {'requests': requests,
                  'tasks': tasks,
                  'actions': actions,
                  'tasks_status': tasks_status,
                  'tasks_performance': tasks_performance,
                  'robots_performance': robots_performance,
                  'rounds': rounds,
                  'bid_times': bid_times}

        MongoStore(db_name=name)
        cls._mongometa.connection_name = name
        cls._mongometa.collection_name = approach
        run_id = cls.get_run_id(new_run)
        experiment = cls(run_id, name, approach, bidding_rule, dataset, **kwargs)
        experiment.save()
        return experiment

    @classmethod
    def get_run_id(cls, new_run):
        run_ids = cls.get_run_ids()
        if new_run:
            run_id = cls.get_new_run(run_ids)
        else:
            run_id = cls.get_current_run(run_ids)
        return run_id

    @classmethod
    def get_run_ids(cls):
        run_ids = [experiment.run_id for experiment in cls.objects.all()]
        return sorted(run_ids)

    @staticmethod
    def get_new_run(run_ids):
        if run_ids:
            previous_run = run_ids.pop()
            next_run = previous_run + 1
        else:
            next_run = 1
        return next_run

    @staticmethod
    def get_current_run(run_ids):
        if run_ids:
            current_run = run_ids.pop()
        else:
            current_run = 1
        return current_run

    @staticmethod
    def get_requests():
        return [request for request in TransportationRequest.objects.all()]

    @staticmethod
    def get_tasks():
        tasks = [task for task in Task.objects.all()]
        with switch_collection(Task, Task.Meta.archive_collection):
            archived_tasks = [task for task in Task.objects.all()]
        return tasks + archived_tasks

    @staticmethod
    def get_actions():
        return [action for action in Action.objects.all()]

    @staticmethod
    def get_tasks_status(tasks):
        tasks_status = list()
        for task in tasks:
            try:
                task_status = TaskStatus.objects.get({"_id": task.task_id})
                task_status.task = task
                task_status.save()
                tasks_status.append(task_status)
            except DoesNotExist:
                pass

        with switch_collection(TaskStatus, TaskStatus.Meta.archive_collection):
            for task in tasks:
                try:
                    task_status = TaskStatus.objects.get({"_id": task.task_id})
                    task_status.task = task
                    task_status.save()
                    tasks_status.append(task_status)
                except DoesNotExist:
                    pass

        return tasks_status

    @staticmethod
    def get_tasks_performance():
        return [task_performance for task_performance in TaskPerformance.objects.all()]

    @staticmethod
    def get_robots_performance():
        return [robot_performance for robot_performance in RobotPerformance.objects.all()]

    @staticmethod
    def get_bid_times(robots_performance):
        bid_times = list()
        for p in robots_performance:
            store = MongoStore(db_name='robot_proxy_store_' + p.robot_id.split('_')[1])
            for bid in BidTime.objects.all():
                bid_times.append(bid)
        return bid_times

    @staticmethod
    def get_rounds():
        return [round_ for round_ in Round.objects.all()]

    @classmethod
    def get_experiments(cls, approach, bidding_rule, dataset):
        with switch_collection(cls, approach):
            by_dataset = [e for e in Experiment.objects.by_dataset(dataset)]
            by_bidding_rule = [e for e in Experiment.objects.by_bidding_rule(bidding_rule)]
            return [e for e in by_dataset if e in by_bidding_rule]

    @classmethod
    def get_experiment(cls, approach, rund_id):
        with switch_collection(cls, approach):
            return cls.objects.by_run_id(rund_id)
