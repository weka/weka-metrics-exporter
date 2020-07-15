# Weka Metrics Exporter

Metrics exporter for WekaFS. Gathers metrics and statistics from a Weka Cluster and exposes a metrics endpoint for Prometheus to scrape.

## Installation

1. Unpack tarball or clone repository.
2. Make sure you have Python 3.5+ and `pip` installed on you machine.
3. Install package dependencies: `pip install -r requirements.txt`.

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
    - targets: ['localhost:8000']
```

*Important*: Currently the exporter pulls stats every 60 seconds so there is no use of setting the interval to a lower value.

It is recommended to be run as a daemon: `nohup ./weka-metrics-exporter.py -a -H <target host> &`. or better, run as a system service.

## Configuration

- Use the `-a` option to load-balance the data collection over all the Weka backends.
- Use `-H` hostname as needed (for example, if you're not running this on a client or backend).
- `-p` port sets the port number - Default port is 8000
- `-h` will print the help message.

Comments, issues, etc can be reported in the repository's issues.

Maintained by vince@weka.io
