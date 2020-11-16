FROM alpine:latest

RUN apk add --no-cache bash curl python3 py3-pip

RUN pip3 install prometheus_client pyyaml
  
COPY weka-metrics-exporter /root/
COPY weka-metrics-exporter.yml /root/
COPY start_exporter.sh /root/
COPY collector.py /root/
COPY reserve.py /root/
COPY signals.py /root/
COPY sthreads.py /root/
COPY wekaapi.py /root/

RUN chmod +x /root/start_exporter.sh /root/weka-metrics-exporter

ENTRYPOINT ["/root/start_exporter.sh"]
