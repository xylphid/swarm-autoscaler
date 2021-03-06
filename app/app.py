#!/usr/bin/env python

from abc import ABC, abstractmethod
from collections import namedtuple
from datetime import datetime, timedelta
from statistics import mean
import argparse
import copy
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
        self.states = {}

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
        parameters = (
            pika.ConnectionParameters(host=os.environ["QUEUE_URL"],
                connection_attempts=5, retry_delay=1)
        )
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        channel.basic_consume(queue='autoscaling.stats', on_message_callback=self.register_stats, auto_ack=True)
        channel.start_consuming()


    def register_stats(self, channel, method, properties, body):
        metrics = json.loads(body,object_hook=lambda d: namedtuple('X', d.keys())(*d.values()));
        self.states[metrics.service.name] = self.states[metrics.service.name] if metrics.service.name in self.states else {}
        self.states[metrics.service.name] = self.mergeDict(self.states[metrics.service.name], {
            "id": metrics.service.id,
            "tasks": {
                metrics.service.task: { "metrics": { metrics.timestamp: { "cpu": metrics.stats.cpu, "mem": metrics.stats.memory} } }
            }
        })

        self.cleanMetrics()
        self.cleanServices()
        self.check_stats()

    def check_stats(self):
        ''' Extract metrics for each service's task '''
        for service_name, content in self.states.items():
            scaleTime = None
            try:
                scaleTime = content.get('scaleTime', None)
            except:
                pass

            cpu, mem = [], []
            for task in content["tasks"].values():
                stats = self.get_stats(task["metrics"])
                cpu.extend(stats[0])
                mem.extend(stats[1])

                if self.isOverloading(cpu, mem, scaleTime):
                    self.scale_up(content["id"])
                    content["scaleTime"] = datetime.now() + timedelta(minutes=5)
                    task["metrics"] = {}
                elif scaleTime is None or scaleTime < datetime.now():
                    self.scale_down(content["id"])
                    content["scaleTime"] = datetime.now() + timedelta(minutes=2)

    def isOverloading(self, cpu, mem, scaleTime):
        limit = 50
        canScale = scaleTime is None or scaleTime < datetime.now()
        cpuOverload = True if len(cpu) and mean(cpu) > limit else False
        memOverload = True if len(mem) and mean(mem) > limit else False
        self.logger.debug(f'ScaleTime : {canScale} - CPU : {len(cpu)} - MEM : {len(mem)}')

        return canScale and (cpuOverload or memOverload)

    def scale_up(self, service_id):
        ''' Scale up if possible (mode = replicated, fully replicated) '''
        service = self.client.services.get(service_id)
        if self.is_service_replicated(service) and self.is_service_fully_replicated(service):
            self.logger.info(f'Scaling up service {self.get_service_name(service)}')
            service.scale(self.get_service_replicas(service) + 1)

    def scale_down(self, service_id):
        ''' Scale down if possible (mode = replicated, scale > 1) '''
        service = self.client.services.get(service_id)
        if self.is_service_replicated(service) and self.get_service_replicas(service) > 1:
            replicas = self.get_service_replicas(service) -1
            self.logger.info(f'Scaling down service {self.get_service_name(service)} ({replicas})')
            service.scale(replicas)

    def get_items(self):
        try:
            return self.client.services.list()
        except:
            self.logger.error("Unable to retreive services")
            return []

    def get_stats(self, item):
        cpu, mem = [], []
        for metrics in item.values():
            cpu.append(metrics["cpu"])
            mem.append(metrics["mem"])

        return cpu, mem

    def is_service_replicated(self, service):
        is_replicated = False
        try:
            is_replicated = service.attrs['Spec']['Mode']['Replicated']
        except:
            pass

        return is_replicated

    def is_service_fully_replicated(self, service):
        ''' Check if service is fully replicated (all tasks are running) '''
        is_fully_replicated = True
        service.reload()
        tasks = service.tasks(filters={"desired-state":"running"})
        for task in tasks:
            is_fully_replicated = is_fully_replicated if task["Status"]["State"] == "running" else False

        return is_fully_replicated

    def get_service_name(self, service):
        return service.attrs['Spec']['Name']

    def get_service_replicas(self, service):
        return service.attrs['Spec']['Mode']['Replicated']['Replicas'];

    def mergeDict(self, dict1, dict2):
        ''' Merge dictionaries and keep values of common keys in list'''
        dict3 = {**dict1, **dict2}
        for key, value in dict3.items():
            if key in dict1 and key in dict2:
                if isinstance(dict3[key], dict):
                    dict3[key] = self.mergeDict(dict1[key], dict2[key])
                elif dict1[key] != dict2[key]:
                    dict3[key] = [value , dict2[key]]
                else: 
                    dict3[key] = value

        return dict3

    def cleanMetrics(self):
        ''' Clean metrics older than 5 minutes '''
        output = copy.deepcopy(self.states)
        for service, content in self.states.items():
            for name, task in content["tasks"].items():
                for timestamp, metrics in task["metrics"].items():
                    if datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S") < (datetime.now() - timedelta(minutes=5)):
                        del(output[service]["tasks"][name]["metrics"][timestamp])

        self.states = output

    def cleanServices(self):
        items = self.get_items();
        services = []
        for item in items:
            services.append(item.name)
        for service in [item for item in self.states.keys() if item not in services]:
            del(self.states[service])

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
            return self.client.containers.list(filters={"status": "running", "label":["io.xylphid.autoscaling=true"]})
        except:
            self.logger.error("Unable to retreive containers")
            return []

    # Compute statistics for a task
    def get_stats(self, item):
        stats = item.stats(stream=False)
        cpu_limit = stats["cpu_stats"]["system_cpu_usage"]
        cpu_usage = stats["cpu_stats"]["cpu_usage"]["total_usage"]
        memory_usage = stats["memory_stats"]["usage"]
        memory_limit = stats["memory_stats"]["limit"]

        computed_stats = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "service": {
                "id": item.attrs["Config"]["Labels"]["com.docker.swarm.service.id"],
                "name": item.attrs["Config"]["Labels"]["com.docker.swarm.service.name"],
                "task": item.attrs["Config"]["Labels"]["com.docker.swarm.task.id"],
            },
            "stats": {
                "cpu"   :   round(float(cpu_usage / cpu_limit * 100), 2),
                "memory":   round(float(memory_usage / memory_limit * 100), 2)
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
        channel.queue_declare(queue="autoscaling.stats", arguments={'x-message-ttl' : 600000})
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
    parser.add_argument("--debug", help="Set debug mode", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    # Monitor loop
    logger.info("Starting autoscaler")
    logger.info(f"Mode : {args.mode}")
    logger.debug(f'Debug mode activated')
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
    LOG_FORMAT = '%(asctime)-15s - %(levelname)s - %(message)s'
    logging.basicConfig(handlers=[logging.StreamHandler(sys.stdout)], format=LOG_FORMAT)
    logger = logging.getLogger("autoscaler")
    logger.setLevel(logging.INFO)

    main(logger)