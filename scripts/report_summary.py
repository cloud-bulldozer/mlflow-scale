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
        'mode': summary.get('mode', ''),
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


# Distinct color palette - hand-picked for maximum contrast
DISTINCT_COLORS = [
    '#e41a1c',  # red
    '#377eb8',  # blue
    '#4daf4a',  # green
    '#984ea3',  # purple
    '#ff7f00',  # orange
    '#a65628',  # brown
    '#f781bf',  # pink
    '#999999',  # grey
    '#17becf',  # cyan
    '#bcbd22',  # olive
]

# Different markers for each operation
MARKERS = ['o', 's', '^', 'D', 'v', 'p', 'h', '*', 'X', 'P']


def _save_chart(filepath):
    """Save current figure to file and close it."""
    plt.tight_layout()
    plt.savefig(filepath, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Chart saved: {filepath}")


def _add_config_column(df):
    """Add a 'config' column with T{tenants}_C{concurrency} format."""
    df = df.copy()
    df['config'] = df.apply(lambda r: f"T{r['tenants']}_C{r['concurrency']}", axis=1)
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


def _plot_response_times_on_axis(ax, group, x_col, operations, metric_suffix, xlabel, ylabel, title):
    """Helper to plot response times on a single axis.
    
    Args:
        ax: Matplotlib axis to plot on
        group: DataFrame with the data to plot
        x_col: Column name for x-axis values
        operations: List of operation names
        metric_suffix: Suffix for metric columns (e.g., '_avg_ms' or '_p95_ms')
        xlabel: Label for x-axis
        ylabel: Label for y-axis
        title: Chart title
    """
    for op_idx, op in enumerate(operations):
        color = DISTINCT_COLORS[op_idx % len(DISTINCT_COLORS)]
        marker = MARKERS[op_idx % len(MARKERS)]
        col = f'{op}{metric_suffix}'
        if col in group.columns:
            ax.plot(group[x_col], group[col], marker=marker, linestyle='-',
                    label=op, color=color, markersize=8, linewidth=2)
    
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.set_ylim(bottom=0)
    ax.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=8)
    ax.grid(True, alpha=0.3)


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


def plot_response_times_by_concurrency(df, output_dir="."):
    """Plot P95 response times vs concurrency for each tenant configuration."""
    operations = get_operation_names(df)
    p95_cols = [f'{op}_p95_ms' for op in operations if f'{op}_p95_ms' in df.columns]
    y_max = _safe_max(df[p95_cols].max().max()) if p95_cols else None
    if y_max is not None:
        y_max *= 1.1
    
    def plot_func(ax, group, x_col, tenants):
        _plot_response_times_on_axis(
            ax, group, x_col, operations, '_p95_ms',
            'Concurrency', 'P95 Response Time (ms)',
            f'P95 Response Times - {tenants} Tenant(s)'
        )
    
    _plot_grouped_subplots(df, 'tenants', 'concurrency', plot_func, 
                           'chart_response_times_by_concurrency.png', output_dir, y_max=y_max)


def plot_response_times_by_tenants(df, output_dir="."):
    """Plot P95 response times vs tenants for each concurrency configuration."""
    operations = get_operation_names(df)
    p95_cols = [f'{op}_p95_ms' for op in operations if f'{op}_p95_ms' in df.columns]
    y_max = _safe_max(df[p95_cols].max().max()) if p95_cols else None
    if y_max is not None:
        y_max *= 1.1
    
    def plot_func(ax, group, x_col, concurrency):
        _plot_response_times_on_axis(
            ax, group, x_col, operations, '_p95_ms',
            'Tenants', 'P95 Response Time (ms)',
            f'P95 Response Times - {concurrency} Concurrency'
        )
    
    _plot_grouped_subplots(df, 'concurrency', 'tenants', plot_func,
                           'chart_response_times_by_tenants.png', output_dir, y_max=y_max)


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


def _plot_mlflow_cpu_on_axis(ax, group, x_col, xlabel, title):
    """Helper to plot MLflow CPU utilization on a single axis."""
    if 'mlflow_cpu' not in group.columns:
        return
    
    ax.plot(group[x_col], group['mlflow_cpu'], marker='o', linestyle='-',
            color=DISTINCT_COLORS[0], markersize=8, linewidth=2)
    
    ax.set_xlabel(xlabel)
    ax.set_ylabel('CPU (cores)')
    ax.set_title(title)
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.3)


def plot_mlflow_cpu_by_concurrency(metrics_df, output_dir="."):
    """Plot MLflow avg CPU utilization vs concurrency for each tenant configuration."""
    mlflow_cpu = _prepare_mlflow_cpu_data(metrics_df)
    if mlflow_cpu is None:
        return
    y_max = _safe_max(mlflow_cpu['mlflow_cpu'])
    if y_max is not None:
        y_max *= 1.1
    
    def plot_func(ax, group, x_col, tenants):
        _plot_mlflow_cpu_on_axis(ax, group, x_col, 'Concurrency',
                                  f'MLflow Avg CPU - {tenants} Tenant(s)')
    
    _plot_grouped_subplots(mlflow_cpu, 'tenants', 'concurrency', plot_func,
                           'chart_mlflow_cpu_by_concurrency.png', output_dir, y_max=y_max)


def plot_mlflow_cpu_by_tenants(metrics_df, output_dir="."):
    """Plot MLflow avg CPU utilization vs tenants for each concurrency configuration."""
    mlflow_cpu = _prepare_mlflow_cpu_data(metrics_df)
    if mlflow_cpu is None:
        return
    y_max = _safe_max(mlflow_cpu['mlflow_cpu'])
    if y_max is not None:
        y_max *= 1.1
    
    def plot_func(ax, group, x_col, concurrency):
        _plot_mlflow_cpu_on_axis(ax, group, x_col, 'Tenants',
                                  f'MLflow Avg CPU - {concurrency} Concurrency')
    
    _plot_grouped_subplots(mlflow_cpu, 'concurrency', 'tenants', plot_func,
                           'chart_mlflow_cpu_by_tenants.png', output_dir, y_max=y_max)


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
    
    # Ensure output directory exists
    os.makedirs(args.output_dir, exist_ok=True)
    
    print("=" * 60)
    print("MLflow Performance Test Report Generator")
    print("=" * 60)
    
    # Load all summary files
    print(f"\nSearching for files matching: {args.pattern}")
    summaries = load_summary_files(args.pattern)
    print(f"Found {len(summaries)} summary file(s)")
    
    # Create DataFrame
    df = create_dataframe(summaries)
    print(f"\nExtracted metrics for {len(df)} test configurations")
    print(f"Tenants: {sorted(df['tenants'].unique())}")
    print(f"Concurrency: {sorted(df['concurrency'].unique())}")
    
    # Save CSV
    csv_path = os.path.join(args.output_dir, args.csv_name)
    save_csv(df, csv_path)
    
    # Display summary table
    print("\n" + "=" * 60)
    print("Summary Table:")
    print("=" * 60)
    display_cols = ['tenants', 'concurrency', 'http_reqs_total', 'http_reqs_rate']
    display_cols = [c for c in display_cols if c in df.columns]
    print(df[display_cols].to_string(index=False))
    
    # Generate charts
    print("\n" + "=" * 60)
    print("Generating Charts...")
    print("=" * 60)
    
    plot_summary_dashboard(df, args.output_dir)
    plot_response_times_by_concurrency(df, args.output_dir)
    plot_response_times_by_tenants(df, args.output_dir)
    plot_throughput_heatmap(df, args.output_dir)
    plot_passed_counts(df, args.output_dir)
    plot_response_times_p95_heatmap(df, args.output_dir)
    
    # Load and process Prometheus metrics if available
    print("\n" + "=" * 60)
    print("Processing Prometheus Metrics...")
    print("=" * 60)
    
    print(f"\nSearching for metrics files matching: {args.metrics_pattern}")
    metrics_df = load_metrics_csv_files(args.metrics_pattern)
    
    if metrics_df is not None:
        print(f"Found metrics for {len(metrics_df['experiment'].unique())} experiment(s)")
        print(f"Components: {sorted(metrics_df['component'].unique())}")
        
        # Generate resource utilization charts
        print("\nGenerating resource utilization charts...")
        plot_cpu_utilization(metrics_df, args.output_dir)
        plot_memory_utilization(metrics_df, args.output_dir)
        plot_mlflow_cpu_by_concurrency(metrics_df, args.output_dir)
        plot_mlflow_cpu_by_tenants(metrics_df, args.output_dir)
    else:
        print("No Prometheus metrics files found - skipping resource utilization charts")
    
    print("\n" + "=" * 60)
    print("Report generation complete!")
    print("=" * 60)


if __name__ == '__main__':
    main()
