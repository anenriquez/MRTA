import yaml
import logging
from allocation.api.zyre import ZyreAPI

logging.getLogger(__name__)


class Config(object):
    def __init__(self, config_file):
        config = Config.load_file(config_file)
        self.config_params = dict()
        self.config_params.update(**config)

    def configure_api(self, node_name):
        api_config = self.config_params.get('api')
        zyre_config = api_config.get('zyre')
        zyre_config['node_name'] = node_name
        zyre_api = ZyreAPI(zyre_config)
        return zyre_api

    def configure_auctioneer(self):
        logging.info("Configuring auctioneer...")
        allocation_config = self.config_params.get("task_allocation")
        fleet = self.config_params.get('fleet')
        bidding_rule = allocation_config.get('bidding_rule')
        api = self.configure_api('auctioneer')
        request_alternative_timeslots = allocation_config.get('request_alternative_timeslots')
        auction_time = allocation_config.get('auction_time')

        return {'bidding_rule': bidding_rule,
                'robot_ids': fleet,
                'api': api,
                'request_alternative_timeslots': request_alternative_timeslots,
                'auction_time': auction_time
               }

    def configure_allocation_requester(self):
        logging.info("Configuring allocation requester...")
        api = self.configure_api('allocation_requester')
        return {'api': api}

    def configure_robot_proxy(self, robot_id):
        logging.info("Configuring robot %s...", robot_id)
        allocation_config = self.config_params.get('task_allocation')
        api_config = self.config_params.get('api')
        api_config['zyre']['node_name'] = robot_id

        return {'robot_id': robot_id,
                'bidding_rule': allocation_config.get('bidding_rule'),
                'stp_method': allocation_config.get('stp_method'),
                'api_config': api_config,
                'auctioneer': 'auctioneer'
                }

    @staticmethod
    def load_file(config_file):
        file_handle = open(config_file, 'r')
        data = yaml.safe_load(file_handle)
        file_handle.close()
        return data
