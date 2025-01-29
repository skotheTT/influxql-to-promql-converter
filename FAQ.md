
* Annotations if using influxDB then migration is not supported and will need to manually update the dashboards

<table>
  <thead>
    <tr>
      <th rowspan="2">Influx Aggregation Function</th>
      <th colspan="3">Mimir Metric Aggregation Function by Type </th>
    </tr>
    <tr> 
      <th>Counter Function</th>
      <th>Histogram Function</th>
      <th>Gauge Function</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><code>mean()</code></td>
      <td><code>avg()</code></td>
      <td><code>histogram_quantile(0.5, ...)</code></td>
      <td>N/A</td>
    </tr>
    <tr>
      <td><code>sum()</code></td>
      <td><code>sum()</code></td>
      <td><code>histogram_sum(sum())</code></td>
      <td>N/A</td>
    </tr>
    <tr>
      <td><code>count()</code></td>
      <td><code>count()</code></td>
      <td><code>histogram_count()</code></td>
      <td>N/A</td>
    </tr>
    <tr>
      <td><code>min()</code></td>
      <td><code>min()</code></td>
      <td><code>histogram_quantile(0.0, ...)</code></td>
      <td>N/A</td>
    </tr>
    <tr>
      <td><code>max()</code></td>
      <td><code>max()</code></td>
      <td><code>histogram_quantile(1.0, ...)</code></td>
      <td>N/A</td>
    </tr>
    <tr>
      <td><code>percentile()</code></td>
      <td>N/A</td>
      <td><code>histogram_quantile()</code></td>
      <td>N/A</td>
    </tr>
  </tbody>
</table>

* Sometime influx shows more spikes than corresponding mimir metrics
    - This is due to grouping of data points in mimir.  