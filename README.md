# Swarm Autoscaler

Swarm autoscaler is a lighweight tool autoscaling services on your docker swarm according to cpu and memory statistics.

## Supported tags

- 1.0.0 ([Dockerfile](Dockerfile))
- nightly

## Autoscaling Rules

Swarm autosacler will scale services fullfilling this conditions :
- `io.xylphid.autoscaling=true` container label is declared. All services may not be up/down scaled.
- **CPU** average usage over the last 5 minutes is over 50% **OR**
- **Memory** average usage over the last 5 minutes is over 50%

## How to use this image

### Environments

Here are supported environments variable and its definition :
- `QUEUE_URL` : Message queue url or service name

### RabbitMQ

This project use RabbitMQ messaging service to centralize containers stats.\
```bash
docker service create \
    --name autoscaling-queue \
    --replicas 1 \
    --restart-condition any \
    --update-delay 2m \
    --update-order stop-first \
    --update-parallelism 2 \
    rabbitmq:3.8.5-alpine
```

### Agent

The agent read container stats and populate the message queue.
```bash
docker service create \
    --env QUEUE_URL=autoscaling-queue \
    --name autoscaling-agent \
    --limit-memory 50M
    --mode global \
    --mount "type=bind,source=/var/run/docker.sock,destination=/var/run/docker.sock" \
    --restart-condition any \
    --update-delay 2m \
    --update-order start-first \
    --update-parallelism 2 \
    registry.xylphid.net/swarm-autoscaler:1.0.0 \
    --mode=agent
```

### Orchestrator

The orchestrator consume the queue and compute stats over a 5 minutes delay.
```bash
docker service create \
    --constraint node.role==manager \
    --env QUEUE_URL=autoscaling-queue \
    --name autoscaling-orchestrator \
    --mount "type=bind,source=/var/run/docker.sock,destination=/var/run/docker.sock" \
    --replicas 1 \
    --restart-condition any \
    --update-delay 2m \
    --update-order start-first \
    --update-parallelism 2 \
    registry.xylphid.net/swarm-autoscaler:1.0.0 \
    --mode=orchestrator
```

### Autoscaled service example

```
docker service create \
    --name example-autoscaled \
    --label io.xylphid.autoscaling=true
    --replicas 1
    nginx
```

## How to compose with this image

Example in the repository : [docker-compose.yml](docker-compose.yml) 

## Image inheritance

This docker image inherits from [python](https://hub.docker.com/_/python/) image.