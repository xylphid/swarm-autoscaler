FROM python:3.8.3-alpine3.12

ARG VERSION
ENV VERSION ${VERSION:-nightly}

LABEL maintainer.name="Anthony PERIQUET"
LABEL maintainer.email="anthony@periquet.net"
LABEL version=${VERSION}
LABEL description="Swarm-autoscaling v${VERSION}"

ADD app /opt/swarm-autoscaling
RUN touch /var/log/wait \
    && pip install --upgrade pip \
    && pip install -r /opt/swarm-autoscaling/requirements

ENTRYPOINT [ "python", "-u", "/opt/swarm-autoscaling/app.py" ]
