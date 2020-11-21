FROM alpine:latest

RUN apk add --no-cache bash curl python3 py3-pip

RUN pip3 install prometheus_client pyyaml

ARG BASEDIR="/weka"
ARG ID="472"
ARG USER="weka"

RUN mkdir -p $BASEDIR

WORKDIR $BASEDIR

COPY weka-metrics-exporter $BASEDIR
COPY weka-metrics-exporter.yml $BASEDIR
COPY collector.py $BASEDIR
COPY reserve.py $BASEDIR
COPY signals.py $BASEDIR
COPY sthreads.py $BASEDIR
COPY wekaapi.py $BASEDIR
COPY wekacluster.py $BASEDIR

RUN addgroup -S -g $ID $USER &&\
    adduser -S -u $ID -G $USER $USER && \
    chown -R $USER:$USER $BASEDIR

RUN chmod +x $BASEDIR/weka-metrics-exporter

EXPOSE 8001

USER $USER
ENTRYPOINT ["./weka-metrics-exporter"]
