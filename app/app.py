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

logging.basicConfig(handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("autoscaler")
logger.setLevel(logging.DEBUG)

class DockerHelper(ABC):
    def __init__(self, client=docker.from_env()):
        self.client = client
        self.states = []

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
            logger.debug(stats)
            self.send_to_queue(stats)

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

    @abstractmethod
    def get_items(self):
        pass

    @abstractmethod
    def get_stats(self):
        pass

class ServiceHelper(DockerHelper):
    def __init__(self, client):
        DockerHelper.__init__(self, client)

    def get_items(self):
        try:
            services = self.client.services.list()
            for service in services:
                tasks = service.tasks()
                logging.debug( tasks )

            return []
        except:
            return []

    def get_stats(self, item):
        return {}

class ContainerHelper(DockerHelper):
    def __init__(self, client):
        DockerHelper.__init__(self, client)

    def get_items(self):
        try:
            return self.client.containers.list(filters={"status": "running"})
        except:
            return []

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
        # return item.stats(stream=False)

class AutoScalingManager:
    def __init__(self, client=docker.from_env(), default_module="containers"):
        self.modules = {
            "containers"    : ContainerHelper(client),
            "services"      : ServiceHelper(client)
        }

        self.default_module = default_module

    def monitor(self, module=None, delay=None):
        module = self.default_module if not module else module
        helper = self.modules[module]

        helper.monitor()

def main():
    # React on signal
    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGTERM, terminate)

    # Define and parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor", "-m", type=str, help="Monitor filter", choices=["containers", "services"], default="containers")
    parser.add_argument("--delay", "-d", type=int, help="Healthcheck delay (seconds)", default=10)
    args = parser.parse_args()

    # Monitor loop
    while 1:
        watcher = AutoScalingManager()
        watcher.monitor(module=args.monitor, delay=args.delay)
        time.sleep(args.delay)
        del watcher

def terminate(signal, frame):
    logger.info("Shutting down monitor...")
    sys.exit(0)

if __name__ == "__main__":
    main()