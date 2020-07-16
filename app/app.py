#!/usr/bin/env python

from abc import ABC, abstractmethod
from datetime import datetime
import argparse
import docker
import json
import logging
import os
import pika
import signal
import sys
import time

class DockerHelper(ABC):
    def __init__(self, client=docker.from_env(), logger=None):
        self.client = client
        self.logger = logging.getLogger("autoscaler") if logger is None else logger
        self.states = []

    @abstractmethod
    def monitor(self):
        pass

    @abstractmethod
    def get_items(self):
        pass

    @abstractmethod
    def get_stats(self, item):
        pass

class ServiceHelper(DockerHelper):
    def __init__(self, client, logger=None):
        DockerHelper.__init__(self, client, logger=logger)

    def monitor(self):
        items = self.get_items()
        for item in items:
            self.logger.debug(item.attrs)
            # tasks = item.tasks()

    def get_items(self):
        try:
            return self.client.services.list()
        except:
            self.logger.error("Unable to retreive services")
            return []

    def get_stats(self, item):
        return {}

class NodeHelper(DockerHelper):
    def __init__(self, client, logger=None):
        DockerHelper.__init__(self, client, logger=logger)

    def monitor(self):
        items = self.get_items()
        for item in items:
            is_task = False
            try:
                is_task = item.attrs["Config"]["Labels"]["com.docker.swarm.task.id"]
            except:
                pass
            if not is_task:
                continue

            stats = self.get_stats(item)
            self.logger.debug(stats)
            self.send_to_queue(stats)

    # Get running containers
    def get_items(self):
        try:
            return self.client.containers.list(filters={"status": "running"})
        except:
            self.logger.error("Unable to retreive containers")
            return []

    # Compute statistics for a task
    def get_stats(self, item):
        stats = item.stats(stream=False)
        cpu_usage = stats["cpu_stats"]["system_cpu_usage"]
        cpu_total = stats["cpu_stats"]["cpu_usage"]["total_usage"]

        computed_stats = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "service": {
                "id": item.attrs["Config"]["Labels"]["com.docker.swarm.service.id"],
                "name": item.attrs["Config"]["Labels"]["com.docker.swarm.service.name"],
                "task": item.attrs["Config"]["Labels"]["com.docker.swarm.task.id"],
            },
            "stats": {
                "cpu"   :   round(float(cpu_total / cpu_usage * 100), 2),
                "memory":   ""
            }
        }
        return computed_stats


    def send_to_queue(self, message):
        parameters = (
            pika.ConnectionParameters(host=os.environ["QUEUE_URL"],
                connection_attempts=5, retry_delay=1)
        )
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        channel.queue_declare(queue="autoscaling.stats")
        channel.basic_publish(exchange='', routing_key="autoscaling.stats", body=json.dumps(message))
        connection.close()

class AutoScalingManager:
    def __init__(self, client=docker.from_env(), default_mode="agent", logger=None):
        self.modes = {
            "agent"         : NodeHelper(client, logger=logger),
            "orchestrator"  : ServiceHelper(client, logger=logger)
        }

        self.default_mode = default_mode

    def monitor(self, mode=None, delay=None):
        mode = self.default_mode if not mode else mode
        helper = self.modes[mode]

        helper.monitor()

def main(logger):
    # React on signal
    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGTERM, terminate)

    # Define and parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", "-m", type=str, help="Mode selection", choices=["agent", "orchestrator"], default="orchestrator")
    parser.add_argument("--delay", "-d", type=int, help="Healthcheck delay (seconds)", default=10)
    args = parser.parse_args()

    # Monitor loop
    while 1:
        watcher = AutoScalingManager(logger=logger)
        watcher.monitor(mode=args.mode, delay=args.delay)
        time.sleep(args.delay)
        del watcher

def terminate(signal, frame):
    logger.info("Shutting down monitor...")
    sys.exit(0)

if __name__ == "__main__":
    # Define logger
    logging.basicConfig(handlers=[logging.StreamHandler(sys.stdout)])
    logger = logging.getLogger("autoscaler")
    logger.setLevel(logging.DEBUG)

    main(logger)