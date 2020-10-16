# Weka Metrics Exporter

Metrics exporter for WekaFS. Gathers metrics and statistics from a Weka Cluster and exposes a metrics endpoint for Prometheus to scrape.

## Installation

1. Unpack tarball or clone repository.
2. Make sure you have Python 3.5+ and `pip` installed on you machine.
3. Install package dependencies: `pip install -r requirements.txt`.

## New in v2.0
The host specified with the -H parameter may now be a comma-separated list of hosts.  ie: weka1,weka2,weka3   

This will prevent the exporter from failing to start up if the host is down, similar to the way stateless client mounts work.

Fixed an error where old stats that had 0 values were still being reported with their last known value by prometheus

Performance improvements

## Metrics Exported

1. Edit the weka-metrics-exporter.yml file. All the metrics that can be collected are defined there. Note that the default contents are sufficient to populate the example dashboards. **Add ONLY what you need**. It does generate load on the cluster to collects these stats.
2. Uncomment any metrics you would like to gather that are commented out. Restart the exporter to start gathering those metrics.
3. To display new metrics in Grafana, add new panel(s) and query. Try to curl the data from the metrics endpoint.

## Usage

Configure prometheus to pull data from the Weka metrics exporter (see prometheus docs). Example Prometheus configuration:

```
# Weka
- job_name: 'weka'
  scrape_interval: 60s
  static_configs:
    - targets: ['localhost:8001']
```

*Important*: Currently the exporter pulls stats every 60 seconds so there is no use of setting the interval to a lower value.

It is recommended to be run as a daemon: `nohup ./weka-metrics-exporter -a -H <target host> &`. or better, run as a system service.

## Configuration

- Use the `-a` option to load-balance the data collection over all the Weka backends.
- Use `-H` hostname as needed (for example, if you're not running this on a client or backend).
- `-p` port sets the port number - Default port is 8001
- `-h` will print the help message.

## Docker build instructions (optional)

To build this repositiory:

```
git clone <this repo>
docker build --tag weka-metrics-exporter .
```
or
```docker build --tag weka-metrics-exporter https://github.com/weka/weka-metrics-exporter.git```

To save and/or load the image
```
docker save -o weka-metrics-exporter.tar weka-metrics-exporter
docker load -i  weka-metrics-exporter.tar
```

## Running the pre-built Docker Container

If you download the pre-built docker container, it may be loaded with:

```
docker load -i  weka-metrics-exporter.tar
```

Then run with the usual Docker Run command.

## Docker Run Commands

There are a variety of options when running the conatiner.

To ensure that the container has access to DNS, use the ```--network=host``` directive

If you do not use the ```--network=host```, then you might want to map /etc/hosts into the container with: ```--mount type=bind,source=/etc/hosts,target=/etc/hosts```

If you have changed the default password on the cluster, you will need to pass authentication tokens to the container with ```--mount type=bind,source=/root/.weka/auth-token.json,target=/root/.weka/auth-token.json```.  Use the ```weka user login ``` command to generate the tokens, which are stored in ```~/.weka/auth-token.json```

To have messages logged via syslog on the docker host, use ```--mount type=bind,source=/dev/log,target=/dev/log```

If you would like to change the metrics gathered, you can modify the weka-metrics-exporter.yml configuration file, then map that into the container with ```--mount type=bind,source=$PWD/weka-metrics-exporter.yml,target=/root/weka-metrics-exporter.yml```

```
docker run -d --network=host \
  --mount type=bind,source=/root/.weka/auth-token.json,target=/root/.weka/auth-token.json \
  --mount type=bind,source=/dev/log,target=/dev/log \
  --mount type=bind,source=/etc/hosts,target=/etc/hosts \
  --mount type=bind,source=$PWD/weka-metrics-exporter.yml,target=/root/weka-metrics-exporter.yml \
  weka-metrics-exporter <wekahost>,<wekahost>,<wekahost>
```

Comments, issues, etc can be reported in the repository's issues.

Note: This is provided via the GPLv3 license agreement (text in LICENSE.md).  Using/copying this code implies you accept the agreement.

Maintained by vince@weka.io
