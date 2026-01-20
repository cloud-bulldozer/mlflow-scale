#!/usr/bin/env python3
"""
MLflow Performance Test Report Generator

Reads multiple k6 test result JSON files and generates:
1. CSV summary with response times, pass/fail counts, and request rates
2. Charts visualizing metrics across different concurrency and tenant configurations
3. Resource utilization charts from Prometheus metrics CSV files
"""

import json
import glob
import os
import re
import sys
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

def load_summary_files(pattern="summary_*.json"):
    """Load all summary JSON files matching the pattern."""
    files = glob.glob(pattern)
    if not files:
        print(f"No files found matching pattern: {pattern}")
        sys.exit(1)
    
    summaries = []
    for filepath in files:
        with open(filepath, 'r') as f:
            data = json.load(f)
            summaries.append(data)
        print(f"Loaded: {filepath}")
    
    return summaries


def load_metrics_csv_files(pattern="metrics_*.csv"):
    """Load all metrics CSV files matching the pattern.
    
    Returns a DataFrame with columns: experiment, component, metric, aggregation, unit, value
    """
    files = glob.glob(pattern)
    if not files:
        print(f"No metrics files found matching pattern: {pattern}")
        return None
    
    all_metrics = []
    for filepath in files:
        # Extract experiment name from filename (e.g., metrics_1_concurrency_10.csv -> 1_concurrency_10)
        filename = os.path.basename(filepath)
        match = re.match(r'metrics_(.+)\.(csv|log)', filename)
        if match:
            experiment = match.group(1)
        else:
            experiment = filename
        
        # Read CSV, skipping comment lines
        try:
            df = pd.read_csv(filepath, comment='#')
            df['experiment'] = experiment
            all_metrics.append(df)
            print(f"Loaded metrics: {filepath}")
        except Exception as e:
            print(f"Warning: Could not load {filepath}: {e}")
            continue
    
    if not all_metrics:
        return None
    
    # Combine all metrics into a single DataFrame
    metrics_df = pd.concat(all_metrics, ignore_index=True)
    
    # Parse experiment name to extract tenants and concurrency if possible
    # Expected format: {tenants}_concurrency_{concurrency} (e.g., "1_concurrency_10", "10_concurrency_50")
    def parse_experiment(exp):
        match = re.match(r'(\d+)_concurrency_(\d+)', exp)
        if match:
            return int(match.group(1)), int(match.group(2))
        return None, None
    
    metrics_df[['tenants', 'concurrency']] = metrics_df['experiment'].apply(
        lambda x: pd.Series(parse_experiment(x))
    )
    
    return metrics_df


def extract_metrics(summary):
    """Extract relevant metrics from a single summary."""
    metrics = summary.get('data', {}).get('metrics', {})
    
    result = {
        'tenants': summary.get('tenants', 0),
        'concurrency': summary.get('concurrency', 0),
    }
    
    # Process all metrics in a single pass
    for metric_name, metric_data in metrics.items():
        values = metric_data.get('values', {})
        metric_type = metric_data.get('type')
        
        # Response time metrics (trend type)
        if metric_name.endswith('_response_time') and metric_type == 'trend':
            base_name = metric_name.replace('_response_time', '')
            result[f'{base_name}_avg_ms'] = values.get('avg', 0)
            result[f'{base_name}_p90_ms'] = values.get('p(90)', 0)
            result[f'{base_name}_p95_ms'] = values.get('p(95)', 0)
            result[f'{base_name}_max_ms'] = values.get('max', 0)
        
        # Passed/failed counters
        elif metric_name.endswith('_passed') and metric_type == 'counter':
            base_name = metric_name.replace('_passed', '')
            result[f'{base_name}_passed'] = values.get('count', 0)
            result[f'{base_name}_rps'] = values.get('rate', 0)
        
        elif metric_name.endswith('_failed') and metric_type == 'counter':
            base_name = metric_name.replace('_failed', '')
            result[f'{base_name}_failed'] = values.get('count', 0)
    
    # Extract http_reqs rate and count
    if 'http_reqs' in metrics:
        http_reqs = metrics['http_reqs'].get('values', {})
        result['http_reqs_total'] = http_reqs.get('count', 0)
        result['http_reqs_rate'] = http_reqs.get('rate', 0)
    
    # Extract http_req_failed rate
    if 'http_req_failed' in metrics:
        http_failed = metrics['http_req_failed'].get('values', {})
        result['http_req_failed_rate'] = http_failed.get('rate', 0)
        result['http_req_failed_count'] = http_failed.get('passes', 0)
    
    return result


def create_dataframe(summaries):
    """Create a pandas DataFrame from all summaries."""
    records = [extract_metrics(s) for s in summaries]
    df = pd.DataFrame(records)
    
    # Sort by tenants and concurrency
    df = df.sort_values(['tenants', 'concurrency']).reset_index(drop=True)
    
    return df


def save_csv(df, output_path="report_summary.csv"):
    """Save the DataFrame to CSV."""
    df.to_csv(output_path, index=False)
    print(f"\nCSV report saved to: {output_path}")


def save_p95_csv(df, output_path="report_p95_latencies.csv"):
    """Save a CSV with only P95 latency columns."""
    id_cols = ['tenants', 'concurrency']
    p95_cols = [col for col in df.columns if col.endswith('_p95_ms')]
    cols = id_cols + sorted(p95_cols)
    df[cols].to_csv(output_path, index=False)
    print(f"P95 latencies CSV saved to: {output_path}")


def save_rps_csv(df, output_path="report_rps.csv"):
    """Save a CSV with only RPS columns."""
    id_cols = ['tenants', 'concurrency']
    rps_cols = [col for col in df.columns if col.endswith('_rps')]
    # Also include the overall http_reqs_rate if present
    if 'http_reqs_rate' in df.columns:
        rps_cols = ['http_reqs_rate'] + rps_cols
    cols = id_cols + sorted(rps_cols)
    df[cols].to_csv(output_path, index=False)
    print(f"RPS CSV saved to: {output_path}")


# Operation categories for latency analysis
OPERATION_CATEGORIES = {
    'read': ['get_run', 'get_experiment', 'fetch_artifact', 'list_artifacts', 'list_workspaces'],
    'write': ['create_run', 'create_experiment', 'log_metric', 'log_parameter', 'log_artifact', 
              'update_run_status', 'create_prompt', 'create_prompt_version'],
    'search': ['search_runs', 'search_experiments', 'search_prompts'],
}


def _get_operation_category(operation):
    """Get the category for an operation."""
    for category, ops in OPERATION_CATEGORIES.items():
        if operation in ops:
            return category
    return 'other'


def _calculate_pct_change(baseline, value):
    """Calculate percentage change from baseline to value."""
    if baseline is None or value is None or baseline == 0:
        return None
    return round(((value - baseline) / baseline) * 100, 1)


def save_latency_analysis_csv(df, output_dir="."):
    """Generate latency analysis CSVs showing impact of tenant count on each operation.
    
    Creates two files:
    1. latency_analysis_by_tenants.csv - Detailed breakdown by operation
    2. latency_analysis_summary.csv - Aggregated by category
    """
    # Get unique tenant counts and concurrency levels
    tenant_counts = sorted(df['tenants'].unique())
    concurrency_levels = sorted(df['concurrency'].unique())
    
    if len(tenant_counts) < 2:
        print("Skipping latency analysis - need at least 2 tenant configurations")
        return
    
    # Find baseline (minimum tenant count) and comparison targets
    baseline_tenants = min(tenant_counts)
    
    # Get all operations that have P95 metrics
    operations = []
    for col in df.columns:
        if col.endswith('_p95_ms'):
            op = col.replace('_p95_ms', '')
            operations.append(op)
    operations = sorted(operations)
    
    if not operations:
        print("Skipping latency analysis - no P95 metrics found")
        return
    
    # Build detailed analysis records
    detailed_records = []
    
    for op in operations:
        p95_col = f'{op}_p95_ms'
        if p95_col not in df.columns:
            continue
        
        category = _get_operation_category(op)
        
        for concurrency in concurrency_levels:
            record = {
                'operation': op,
                'category': category,
                'concurrency': concurrency,
            }
            
            # Get P95 value for each tenant count
            baseline_value = None
            for tenants in tenant_counts:
                row = df[(df['tenants'] == tenants) & (df['concurrency'] == concurrency)]
                if not row.empty:
                    value = round(row[p95_col].values[0], 2)
                    record[f'{tenants}_tenant{"s" if tenants > 1 else ""}_p95_ms'] = value
                    if tenants == baseline_tenants:
                        baseline_value = value
            
            # Calculate percentage changes from baseline
            for tenants in tenant_counts:
                if tenants == baseline_tenants:
                    continue
                col_name = f'{tenants}_tenant{"s" if tenants > 1 else ""}_p95_ms'
                if col_name in record and baseline_value:
                    pct_change = _calculate_pct_change(baseline_value, record[col_name])
                    record[f'change_{baseline_tenants}_to_{tenants}_pct'] = pct_change
            
            detailed_records.append(record)
    
    # Create detailed DataFrame and save
    detailed_df = pd.DataFrame(detailed_records)
    detailed_path = os.path.join(output_dir, 'report_latency_analysis_by_tenants.csv')
    detailed_df.to_csv(detailed_path, index=False)
    print(f"Latency analysis (detailed) saved to: {detailed_path}")
    
    # Build summary by category
    summary_records = []
    categories = sorted(set(r['category'] for r in detailed_records))
    
    for category in categories:
        category_records = [r for r in detailed_records if r['category'] == category]
        
        for concurrency in concurrency_levels:
            conc_records = [r for r in category_records if r['concurrency'] == concurrency]
            if not conc_records:
                continue
            
            summary = {
                'category': category,
                'concurrency': concurrency,
            }
            
            # Average P95 for each tenant count
            for tenants in tenant_counts:
                col_name = f'{tenants}_tenant{"s" if tenants > 1 else ""}_p95_ms'
                values = [r[col_name] for r in conc_records if col_name in r and r[col_name] is not None]
                if values:
                    summary[f'avg_{tenants}_tenant{"s" if tenants > 1 else ""}_p95_ms'] = round(np.mean(values), 2)
            
            # Average percentage changes
            for tenants in tenant_counts:
                if tenants == baseline_tenants:
                    continue
                pct_col = f'change_{baseline_tenants}_to_{tenants}_pct'
                values = [r[pct_col] for r in conc_records if pct_col in r and r[pct_col] is not None]
                if values:
                    summary[f'avg_change_{baseline_tenants}_to_{tenants}_pct'] = round(np.mean(values), 1)
            
            summary_records.append(summary)
    
    # Create summary DataFrame and save
    summary_df = pd.DataFrame(summary_records)
    summary_path = os.path.join(output_dir, 'report_latency_analysis_summary.csv')
    summary_df.to_csv(summary_path, index=False)
    print(f"Latency analysis (summary) saved to: {summary_path}")


# Distinct color palette - hand-picked for maximum contrast
DISTINCT_COLORS = [
    '#e41a1c',  # red
    '#377eb8',  # blue
    '#4daf4a',  # green
    '#984ea3',  # purple
    '#ff7f00',  # orange
    '#a65628',  # brown
    '#f781bf',  # pink
    '#17becf',  # cyan
    '#bcbd22',  # olive
    '#7f7f7f',  # grey
]

# Different markers for each operation
MARKERS = ['o', 's', '^', 'D', 'v', 'p', 'h', '*', 'X', 'P', '<', '>', '8', 'H']


def get_series_style(index):
    """Get color and marker for a series index, cycling both independently.
    
    Both color and marker cycle with each series but at different rates,
    giving LCM(10, 14) = 70 unique combinations before repeating.
    """
    color = DISTINCT_COLORS[index % len(DISTINCT_COLORS)]
    marker = MARKERS[index % len(MARKERS)]
    return color, marker


def _save_chart(filepath):
    """Save current figure to file and close it."""
    plt.tight_layout()
    plt.savefig(filepath, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Chart saved: {filepath}")


def _add_config_column(df):
    """Add a 'config' column with T{tenants}_C{concurrency} format."""
    df = df.copy()
    df['config'] = df.apply(lambda r: f"T{int(r['tenants'])}_C{int(r['concurrency'])}", axis=1)
    return df


def get_operation_names(df):
    """Extract unique operation names from columns."""
    ops = set()
    for col in df.columns:
        if col.endswith('_avg_ms'):
            ops.add(col.replace('_avg_ms', ''))
        elif col.endswith('_passed'):
            ops.add(col.replace('_passed', ''))
    return sorted(ops)


def _setup_line_axis(ax, xlabel, ylabel, title, show_legend=True):
    """Configure common axis properties for line plots."""
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.set_ylim(bottom=0)
    if show_legend:
        ax.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=8)
    ax.grid(True, alpha=0.3)


def _plot_multi_series(ax, group, x_col, series_configs):
    """Plot multiple series on an axis. series_configs: list of (col, label, color, marker)."""
    for col, label, color, marker in series_configs:
        if col in group.columns:
            ax.plot(group[x_col], group[col], marker=marker, linestyle='-',
                    label=label, color=color, markersize=8, linewidth=2)


def _safe_max(series):
    """Return max of a Series/array, or None if empty/NaN."""
    if series is None:
        return None
    max_val = pd.to_numeric(series, errors='coerce').max()
    if pd.isna(max_val):
        return None
    return float(max_val)


def _plot_grouped_subplots(df, group_by, x_col, plot_func, filename, output_dir=".", y_max=None):
    """Generic helper to create grouped subplot charts.
    
    Args:
        df: DataFrame with data to plot
        group_by: Column name to group by (creates one subplot per group)
        x_col: Column name for x-axis values
        plot_func: Function(ax, group, x_col, group_value) to plot on each axis
        filename: Output filename
        output_dir: Output directory
        y_max: Optional max y-axis value to apply to all subplots
    """
    groups = df.groupby(group_by)
    num_groups = len(groups)
    
    if num_groups == 0:
        return
    
    fig, axes = plt.subplots(1, num_groups, figsize=(7 * num_groups, 6), squeeze=False)
    
    for idx, (group_value, group) in enumerate(groups):
        group = group.sort_values(x_col)
        plot_func(axes[0, idx], group, x_col, group_value)
        if y_max is not None and np.isfinite(y_max):
            axes[0, idx].set_ylim(top=y_max)
    
    _save_chart(os.path.join(output_dir, filename))


def _plot_response_times(df, output_dir, group_by, x_col, xlabel, title_suffix, filename):
    """Generic helper to plot P95 response times grouped by a dimension."""
    operations = get_operation_names(df)
    p95_cols = [f'{op}_p95_ms' for op in operations if f'{op}_p95_ms' in df.columns]
    y_max = _safe_max(df[p95_cols].max().max()) * 1.1 if p95_cols else None
    
    def plot_func(ax, group, x, group_val):
        series = [(f'{op}_p95_ms', op, *get_series_style(i)) for i, op in enumerate(operations)]
        _plot_multi_series(ax, group, x, series)
        _setup_line_axis(ax, xlabel, 'P95 Response Time (ms)', f'P95 Response Times - {group_val} {title_suffix}')
    
    _plot_grouped_subplots(df, group_by, x_col, plot_func, filename, output_dir, y_max=y_max)


def plot_response_times_by_concurrency(df, output_dir="."):
    """Plot P95 response times vs concurrency for each tenant configuration."""
    _plot_response_times(df, output_dir, 'tenants', 'concurrency', 'Concurrency', 
                         'Tenant(s)', 'chart_response_times_by_concurrency.png')


def plot_response_times_by_tenants(df, output_dir="."):
    """Plot P95 response times vs tenants for each concurrency configuration."""
    _plot_response_times(df, output_dir, 'concurrency', 'tenants', 'Tenants',
                         'Concurrency', 'chart_response_times_by_tenants.png')


def _plot_rps(df, output_dir, group_by, x_col, xlabel, title_suffix, filename):
    """Generic helper to plot RPS (requests per second) grouped by a dimension."""
    operations = get_operation_names(df)
    rps_cols = [f'{op}_rps' for op in operations if f'{op}_rps' in df.columns]
    y_max = _safe_max(df[rps_cols].max().max()) * 1.1 if rps_cols else None
    
    def plot_func(ax, group, x, group_val):
        series = [(f'{op}_rps', op, *get_series_style(i)) for i, op in enumerate(operations)]
        _plot_multi_series(ax, group, x, series)
        _setup_line_axis(ax, xlabel, 'Requests per Second', f'RPS - {group_val} {title_suffix}')
    
    _plot_grouped_subplots(df, group_by, x_col, plot_func, filename, output_dir, y_max=y_max)


def plot_rps_by_concurrency(df, output_dir="."):
    """Plot RPS vs concurrency for each tenant configuration."""
    _plot_rps(df, output_dir, 'tenants', 'concurrency', 'Concurrency', 
              'Tenant(s)', 'chart_rps_by_concurrency.png')


def plot_rps_by_tenants(df, output_dir="."):
    """Plot RPS vs tenants for each concurrency configuration."""
    _plot_rps(df, output_dir, 'concurrency', 'tenants', 'Tenants',
              'Concurrency', 'chart_rps_by_tenants.png')


def _plot_heatmap(df, value_col, title, colorbar_label, filename, output_dir=".", value_format=".1f"):
    """Generic helper to plot a heatmap of tenants vs concurrency.
    
    Args:
        df: DataFrame with 'tenants', 'concurrency', and value_col columns
        value_col: Column name to use for heatmap values
        title: Chart title
        colorbar_label: Label for the colorbar
        filename: Output filename
        output_dir: Output directory
        value_format: Format string for value annotations (e.g., '.1f', '.0f')
    """
    if value_col not in df.columns:
        return
    
    pivot = df.pivot_table(index='tenants', columns='concurrency', values=value_col, aggfunc='mean')
    
    if pivot.empty:
        return
    
    fig, ax = plt.subplots(figsize=(10, 6))
    
    im = ax.imshow(pivot.values, cmap='YlOrRd', aspect='auto')
    
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index)
    
    ax.set_xlabel('Concurrency')
    ax.set_ylabel('Tenants')
    ax.set_title(title)
    
    # Add value annotations
    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            val = pivot.values[i, j]
            if not np.isnan(val):
                ax.text(j, i, f'{val:{value_format}}', ha='center', va='center', fontsize=10)
    
    plt.colorbar(im, ax=ax, label=colorbar_label)
    _save_chart(os.path.join(output_dir, filename))


def plot_throughput_heatmap(df, output_dir="."):
    """Plot HTTP request rate as a heatmap of tenants vs concurrency."""
    _plot_heatmap(
        df, 'http_reqs_rate',
        title='HTTP Request Rate (req/s)',
        colorbar_label='Requests/sec',
        filename='chart_throughput_heatmap.png',
        output_dir=output_dir,
        value_format='.1f'
    )


def plot_passed_counts(df, output_dir="."):
    """Plot passed operation counts as grouped bar chart."""
    operations = get_operation_names(df)
    passed_cols = [f'{op}_passed' for op in operations if f'{op}_passed' in df.columns]
    
    if not passed_cols:
        return
    
    df = _add_config_column(df)
    
    fig, ax = plt.subplots(figsize=(12, 6))
    
    x = np.arange(len(df))
    width = 0.8 / len(passed_cols)
    
    colors = plt.cm.Set2(np.linspace(0, 1, len(passed_cols)))
    
    for i, col in enumerate(passed_cols):
        offset = (i - len(passed_cols) / 2 + 0.5) * width
        op_name = col.replace('_passed', '')
        ax.bar(x + offset, df[col], width, label=op_name, color=colors[i])
    
    ax.set_xlabel('Test Configuration (T=Tenants, C=Concurrency)')
    ax.set_ylabel('Passed Count')
    ax.set_title('Successful Operations by Configuration')
    ax.set_xticks(x)
    ax.set_xticklabels(df['config'], rotation=45, ha='right')
    ax.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=8)
    ax.grid(True, alpha=0.3, axis='y')
    
    _save_chart(os.path.join(output_dir, 'chart_passed_counts.png'))


def plot_response_times_p95_heatmap(df, output_dir="."):
    """Plot overall P95 response time as a heatmap of tenants vs concurrency."""
    # Calculate overall P95 as mean of all operation P95s
    operations = get_operation_names(df)
    p95_cols = [f'{op}_p95_ms' for op in operations if f'{op}_p95_ms' in df.columns]
    
    if not p95_cols:
        return
    
    df = df.copy()
    df['overall_p95_ms'] = df[p95_cols].mean(axis=1)
    
    _plot_heatmap(
        df, 'overall_p95_ms',
        title='Overall P95 Response Time (ms)',
        colorbar_label='P95 Response Time (ms)',
        filename='chart_response_times_p95_heatmap.png',
        output_dir=output_dir,
        value_format='.0f'
    )


def _setup_bar_axis(ax, x_labels, values, ylabel, title, color='steelblue'):
    """Helper to set up a bar chart axis with common styling."""
    ax.bar(x_labels, values, color=color)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.tick_params(axis='x', rotation=45)
    ax.grid(True, alpha=0.3, axis='y')


def plot_summary_dashboard(df, output_dir="."):
    """Create a summary dashboard with key metrics."""
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    df = _add_config_column(df)
    
    # 1. HTTP Request Rate
    if 'http_reqs_rate' in df.columns:
        _setup_bar_axis(axes[0, 0], df['config'], df['http_reqs_rate'],
                        'Requests/sec', 'HTTP Request Throughput', 'steelblue')
    
    # 2. Total HTTP Requests
    if 'http_reqs_total' in df.columns:
        _setup_bar_axis(axes[0, 1], df['config'], df['http_reqs_total'],
                        'Total Requests', 'Total HTTP Requests', 'teal')
    
    # 3. HTTP Request Failure Rate (with conditional coloring)
    if 'http_req_failed_rate' in df.columns:
        colors = ['green' if r == 0 else 'orange' if r < 0.05 else 'red' 
                  for r in df['http_req_failed_rate']]
        axes[1, 0].bar(df['config'], df['http_req_failed_rate'] * 100, color=colors)
        axes[1, 0].set_ylabel('Failure Rate (%)')
        axes[1, 0].set_title('HTTP Request Failure Rate')
        axes[1, 0].axhline(y=0, color='green', linestyle='--', alpha=0.5)
        axes[1, 0].tick_params(axis='x', rotation=45)
        axes[1, 0].grid(True, alpha=0.3, axis='y')
    
    # 4. Average Response Time (all operations combined)
    avg_cols = [col for col in df.columns if col.endswith('_avg_ms')]
    if avg_cols:
        overall_avg = df[avg_cols].mean(axis=1)
        _setup_bar_axis(axes[1, 1], df['config'], overall_avg,
                        'Avg Response Time (ms)', 'Overall Average Response Time', 'coral')
    
    plt.suptitle('MLflow Performance Test Summary', fontsize=14, fontweight='bold')
    _save_chart(os.path.join(output_dir, 'chart_summary_dashboard.png'))


def _sort_experiments_numerically(experiments):
    """Sort experiment names numerically by tenants and concurrency.
    
    Expected format: {tenants}_concurrency_{concurrency} (e.g., "1_concurrency_10", "10_concurrency_50")
    """
    def parse_key(exp):
        match = re.match(r'(\d+)_concurrency_(\d+)', exp)
        if match:
            return (int(match.group(1)), int(match.group(2)))
        return (float('inf'), float('inf'))  # Put unparseable at end
    
    return sorted(experiments, key=parse_key)


def _plot_resource_utilization(metrics_df, metric_name, ylabel, title, filename, output_dir=".",
                                value_transform=None, alt_metric_names=None):
    """Generic helper to plot avg resource utilization (CPU or memory) across experiments.
    
    Args:
        metrics_df: DataFrame with metrics data
        metric_name: Name of the metric to filter (e.g., 'cpu', 'memory')
        ylabel: Label for y-axis
        title: Chart title
        filename: Output filename
        output_dir: Output directory
        value_transform: Optional function to transform values (e.g., bytes to MB)
        alt_metric_names: Alternative metric names to check if primary is empty
    """
    if metrics_df is None or metrics_df.empty:
        print(f"No metrics data available for {metric_name} utilization chart")
        return
    
    # Filter for the specified metric
    filtered_df = metrics_df[metrics_df['metric'] == metric_name].copy()
    if filtered_df.empty and alt_metric_names:
        filtered_df = metrics_df[metrics_df['metric'].isin(alt_metric_names)].copy()
    
    if filtered_df.empty:
        print(f"No {metric_name} metrics found")
        return
    
    # Filter for avg only
    filtered_df = filtered_df[filtered_df['aggregation'] == 'avg'].copy()
    
    # Convert value to numeric, handling N/A
    filtered_df['value'] = pd.to_numeric(filtered_df['value'], errors='coerce')
    
    # Apply value transformation if provided
    if value_transform:
        filtered_df['plot_value'] = value_transform(filtered_df['value'])
    else:
        filtered_df['plot_value'] = filtered_df['value']
    
    # Get unique components and experiments
    components = filtered_df['component'].unique()
    experiments = _sort_experiments_numerically(filtered_df['experiment'].unique())
    
    fig, ax = plt.subplots(figsize=(12, 6))
    
    colors = plt.cm.tab10(np.linspace(0, 1, len(components)))
    x = np.arange(len(experiments))
    width = 0.8 / len(components)
    
    for comp_idx, component in enumerate(components):
        comp_data = filtered_df[filtered_df['component'] == component]
        values = []
        for exp in experiments:
            exp_data = comp_data[comp_data['experiment'] == exp]
            if not exp_data.empty and not pd.isna(exp_data['plot_value'].values[0]):
                values.append(exp_data['plot_value'].values[0])
            else:
                values.append(0)
        
        offset = (comp_idx - len(components) / 2 + 0.5) * width
        ax.bar(x + offset, values, width, label=component, color=colors[comp_idx])
    
    ax.set_xlabel('Experiment')
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    if metric_name == 'cpu':
        ax.set_ylim(bottom=0)
    ax.set_xticks(x)
    ax.set_xticklabels(experiments, rotation=45, ha='right')
    ax.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=8)
    ax.grid(True, alpha=0.3, axis='y')
    
    _save_chart(os.path.join(output_dir, filename))


def plot_cpu_utilization(metrics_df, output_dir="."):
    """Plot avg CPU utilization for all components across experiments."""
    _plot_resource_utilization(
        metrics_df, 'cpu',
        ylabel='CPU (cores)',
        title='Avg CPU Utilization by Component',
        filename='chart_cpu_utilization.png',
        output_dir=output_dir,
        alt_metric_names=['cpu', 'cpu_utilization']
    )


def plot_memory_utilization(metrics_df, output_dir="."):
    """Plot avg memory utilization for all components across experiments."""
    _plot_resource_utilization(
        metrics_df, 'memory',
        ylabel='Memory (MB)',
        title='Avg Memory Utilization by Component',
        filename='chart_memory_utilization.png',
        output_dir=output_dir,
        value_transform=lambda v: v / (1024 * 1024)  # Convert bytes to MB
    )


def _prepare_mlflow_cpu_data(metrics_df):
    """Prepare MLflow CPU metrics data for plotting.
    
    Returns:
        DataFrame with mlflow_cpu column, or None if no data available.
    """
    if metrics_df is None or metrics_df.empty:
        print("No metrics data available for MLflow CPU chart")
        return None
    
    # Filter for mlflow CPU avg only
    mlflow_cpu = metrics_df[
        (metrics_df['component'] == 'mlflow') & 
        (metrics_df['metric'] == 'cpu') & 
        (metrics_df['aggregation'] == 'avg')
    ].copy()
    
    if mlflow_cpu.empty:
        print("No MLflow CPU metrics found")
        return None
    
    mlflow_cpu['value'] = pd.to_numeric(mlflow_cpu['value'], errors='coerce')
    mlflow_cpu = mlflow_cpu.rename(columns={'value': 'mlflow_cpu'})
    
    return mlflow_cpu


def _plot_mlflow_cpu(metrics_df, output_dir, group_by, x_col, xlabel, title_suffix, filename):
    """Generic helper to plot MLflow CPU utilization grouped by a dimension."""
    mlflow_cpu = _prepare_mlflow_cpu_data(metrics_df)
    if mlflow_cpu is None:
        return
    y_max = _safe_max(mlflow_cpu['mlflow_cpu']) * 1.1 if _safe_max(mlflow_cpu['mlflow_cpu']) else None
    
    def plot_func(ax, group, x, group_val):
        _plot_multi_series(ax, group, x, [('mlflow_cpu', 'mlflow', *get_series_style(0))])
        _setup_line_axis(ax, xlabel, 'CPU (cores)', f'MLflow Avg CPU - {group_val} {title_suffix}', show_legend=False)
    
    _plot_grouped_subplots(mlflow_cpu, group_by, x_col, plot_func, filename, output_dir, y_max=y_max)


def plot_mlflow_cpu_by_concurrency(metrics_df, output_dir="."):
    """Plot MLflow avg CPU utilization vs concurrency for each tenant configuration."""
    _plot_mlflow_cpu(metrics_df, output_dir, 'tenants', 'concurrency', 'Concurrency',
                     'Tenant(s)', 'chart_mlflow_cpu_by_concurrency.png')


def plot_mlflow_cpu_by_tenants(metrics_df, output_dir="."):
    """Plot MLflow avg CPU utilization vs tenants for each concurrency configuration."""
    _plot_mlflow_cpu(metrics_df, output_dir, 'concurrency', 'tenants', 'Tenants',
                     'Concurrency', 'chart_mlflow_cpu_by_tenants.png')


def _section(title):
    """Print a section header."""
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Generate MLflow performance test reports')
    parser.add_argument('--pattern', '-p', default='summary_*.json',
                        help='Glob pattern for input JSON files (default: summary_*.json)')
    parser.add_argument('--metrics-pattern', '-m', default='metrics_*.csv',
                        help='Glob pattern for metrics CSV files (default: metrics_*.csv)')
    parser.add_argument('--output-dir', '-o', default='.',
                        help='Output directory for CSV and charts (default: current directory)')
    parser.add_argument('--csv-name', '-c', default='report_summary.csv',
                        help='Output CSV filename (default: report_summary.csv)')
    args = parser.parse_args()
    
    os.makedirs(args.output_dir, exist_ok=True)
    _section("MLflow Performance Test Report Generator")
    
    # Load and process summary files
    print(f"\nSearching for files matching: {args.pattern}")
    summaries = load_summary_files(args.pattern)
    df = create_dataframe(summaries)
    print(f"Found {len(summaries)} summary file(s), {len(df)} configurations")
    print(f"Tenants: {sorted(df['tenants'].unique())}, Concurrency: {sorted(df['concurrency'].unique())}")
    
    save_csv(df, os.path.join(args.output_dir, args.csv_name))
    save_p95_csv(df, os.path.join(args.output_dir, 'report_p95_latencies.csv'))
    save_rps_csv(df, os.path.join(args.output_dir, 'report_rps.csv'))
    save_latency_analysis_csv(df, args.output_dir)
    
    # Summary table
    _section("Summary Table")
    display_cols = [c for c in ['tenants', 'concurrency', 'http_reqs_total', 'http_reqs_rate'] if c in df.columns]
    print(df[display_cols].to_string(index=False))
    
    # Generate k6 charts
    _section("Generating Charts")
    for plot_fn in [plot_summary_dashboard, plot_response_times_by_concurrency, plot_response_times_by_tenants,
                    plot_rps_by_concurrency, plot_rps_by_tenants,
                    plot_throughput_heatmap, plot_passed_counts, plot_response_times_p95_heatmap]:
        plot_fn(df, args.output_dir)
    
    # Process Prometheus metrics
    _section("Processing Prometheus Metrics")
    print(f"Searching for: {args.metrics_pattern}")
    metrics_df = load_metrics_csv_files(args.metrics_pattern)
    
    if metrics_df is not None:
        print(f"Found {len(metrics_df['experiment'].unique())} experiment(s), components: {sorted(metrics_df['component'].unique())}")
        for plot_fn in [plot_cpu_utilization, plot_memory_utilization, 
                        plot_mlflow_cpu_by_concurrency, plot_mlflow_cpu_by_tenants]:
            plot_fn(metrics_df, args.output_dir)
    else:
        print("No metrics files found - skipping resource charts")
    
    _section("Report generation complete!")


if __name__ == '__main__':
    main()
