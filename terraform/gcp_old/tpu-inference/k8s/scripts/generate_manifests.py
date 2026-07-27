#!/usr/bin/env python3
import os
import re
import sys
import shutil
from pathlib import Path
from string import Template
import hcl2

def clean_val(val):
    """Clean hcl2 string quotes."""
    if isinstance(val, str):
        return val.strip('"\'')
    return val

def format_extra_node_labels(node_labels):
    """Format node labels dict into indented YAML string for template substitution."""
    if not node_labels:
        return ""
    lines = [""]
    for k, v in sorted(node_labels.items()):
        lines.append(f"    {clean_val(k)}: {clean_val(v)}")
    return "\n".join(lines)

def parse_tfvars(file_path):
    """Parse HCL tfvars file using official hcl2 library."""
    with open(file_path, "r") as f:
        data = hcl2.load(f)

    project_id = clean_val(data.get("project_id", "cloud-ullm-inference-ci-cd"))
    name_prefix = clean_val(data.get("name_prefix", "tpu-ci"))
    manager_region = clean_val(data.get("manager_region", "us-central1"))
    secret_project = clean_val(data.get("buildkite_secret_project", project_id))
    secret_id = clean_val(data.get("buildkite_secret_id", "buildkite-tpu-ci-agent-dev-token"))
    
    raw_worker_clusters = data.get("worker_clusters", {})
    worker_clusters = {}
    
    for key, wdata in raw_worker_clusters.items():
        project = clean_val(wdata.get("project", "cloud-tpu-inference-test"))
        location = clean_val(wdata.get("location", key))
        
        pools = {}
        raw_pools = wdata.get("tpu_pools", {})
        for pk, pdata in raw_pools.items():
            chips = int(pdata.get("chips_per_node", 1))
            max_nodes = int(pdata.get("max_nodes", 16))
            cohort = clean_val(pdata.get("cohort", "v6e-cohort"))
            
            node_labels = {}
            if "node_labels" in pdata and isinstance(pdata["node_labels"], dict):
                node_labels.update(pdata["node_labels"])

            pools[pk] = {
                "chips": chips,
                "max_nodes": max_nodes,
                "quota": chips * max_nodes,
                "cohort": cohort,
                "node_labels": node_labels
            }
            
        worker_clusters[key] = {
            "project": project,
            "location": location,
            "cluster_name": f"{name_prefix}-{key}",
            "profile_name": f"{name_prefix}-{key}-global",
            "pools": pools
        }
        
    return {
        "project_id": project_id,
        "name_prefix": name_prefix,
        "manager_region": manager_region,
        "manager_cluster": f"{name_prefix}-manager",
        "secret_project": secret_project,
        "secret_id": secret_id,
        "namespace": "buildkite",
        "worker_clusters": worker_clusters
    }

def generate_manager_yaml(config, templates):
    doc_parts = []
    
    # 1. Base (Namespace, ClusterSecretStore, ExternalSecret)
    base_tmpl = Template(templates["base"])
    doc_parts.append(base_tmpl.substitute(
        NAMESPACE=config["namespace"],
        SECRET_PROJECT=config["secret_project"],
        SECRET_ID=config["secret_id"],
        CLUSTER_LOCATION=config["manager_region"],
        CLUSTER_NAME=config["manager_cluster"]
    ))
    
    # 2. MultiKueueCluster per worker
    mk_cluster_tmpl = Template(templates["multikueue_cluster"])
    cohort_workers = {}
    
    for wk, wconf in config["worker_clusters"].items():
        wname = wconf["cluster_name"]
        pname = wconf["profile_name"]
        doc_parts.append(mk_cluster_tmpl.substitute(
            WORKER_NAME=wname,
            CLUSTER_PROFILE_NAME=pname
        ))
        
        for pk, pconf in wconf["pools"].items():
            cohort = pconf["cohort"]
            if cohort not in cohort_workers:
                cohort_workers[cohort] = set()
            cohort_workers[cohort].add(wname)

    # 3. MultiKueueConfig & AdmissionCheck per explicit cohort
    mk_config_tmpl = Template(templates["multikueue_config"])
    adm_check_tmpl = Template(templates["admission_check"])
    
    for cohort, workers in cohort_workers.items():
        w_list_str = "\n".join(f"    - {w}" for w in sorted(workers))
        doc_parts.append(mk_config_tmpl.substitute(
            COHORT_NAME=cohort,
            WORKER_LIST=w_list_str
        ))
        doc_parts.append(adm_check_tmpl.substitute(
            COHORT_NAME=cohort
        ))
        
    # 4. ResourceFlavor, ClusterQueue, LocalQueue per pool profile across all workers
    queue_mgr_tmpl = Template(templates["queue_group_manager"])
    pool_data = {}
    
    for wk, wconf in config["worker_clusters"].items():
        for pk, pconf in wconf["pools"].items():
            if pk not in pool_data:
                pool_data[pk] = {"quota": 0, "cohort": pconf["cohort"], "node_labels": pconf["node_labels"]}
            pool_data[pk]["quota"] += pconf["quota"]

    for pk, pinfo in pool_data.items():
        doc_parts.append(queue_mgr_tmpl.substitute(
            PROFILE_NAME=pk,
            COHORT_NAME=pinfo["cohort"],
            NAMESPACE=config["namespace"],
            NOMINAL_QUOTA=pinfo["quota"],
            EXTRA_NODE_LABELS=format_extra_node_labels(pinfo["node_labels"])
        ))

    return "\n".join(doc_parts) + "\n"

def generate_worker_yaml(config, worker_key, templates):
    wconf = config["worker_clusters"][worker_key]
    doc_parts = []
    
    # 1. Base (Namespace, ClusterSecretStore, ExternalSecret)
    base_tmpl = Template(templates["base"])
    doc_parts.append(base_tmpl.substitute(
        NAMESPACE=config["namespace"],
        SECRET_PROJECT=config["secret_project"],
        SECRET_ID=config["secret_id"],
        CLUSTER_LOCATION=wconf["location"],
        CLUSTER_NAME=wconf["cluster_name"]
    ))
    
    # 2. ResourceFlavor, ClusterQueue, LocalQueue per pool profile in worker
    queue_wkr_tmpl = Template(templates["queue_group_worker"])
    
    for pk, pconf in wconf["pools"].items():
        doc_parts.append(queue_wkr_tmpl.substitute(
            PROFILE_NAME=pk,
            COHORT_NAME=pconf["cohort"],
            NAMESPACE=config["namespace"],
            NOMINAL_QUOTA=pconf["quota"],
            EXTRA_NODE_LABELS=format_extra_node_labels(pconf["node_labels"])
        ))

    return "\n".join(doc_parts) + "\n"

def load_templates(templates_dir):
    templates = {}
    templates["base"] = (templates_dir / "base.yaml.tpl").read_text()
    templates["multikueue_cluster"] = (templates_dir / "multikueue_cluster.yaml.tpl").read_text()
    templates["multikueue_config"] = (templates_dir / "multikueue_config.yaml.tpl").read_text()
    templates["admission_check"] = (templates_dir / "admission_check.yaml.tpl").read_text()
    templates["queue_group_manager"] = (templates_dir / "queue_group_manager.yaml.tpl").read_text()
    templates["queue_group_worker"] = (templates_dir / "queue_group_worker.yaml.tpl").read_text()
    return templates

def main():
    k8s_dir = Path(__file__).resolve().parent.parent
    tfvars_file = k8s_dir / "prod.auto.tfvars"
    templates_dir = k8s_dir / "kueue" / "templates"
    out_dir = k8s_dir / "generated"

    if not tfvars_file.exists():
        print(f"Error: {tfvars_file} not found.")
        sys.exit(1)

    print(f"Reading configuration from (using official hcl2 parser): {tfvars_file}")
    config = parse_tfvars(tfvars_file)

    print(f"Loading template files from: {templates_dir}")
    templates = load_templates(templates_dir)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate Manager Manifest
    mgr_yaml = generate_manager_yaml(config, templates)
    mgr_file = out_dir / "manager.yaml"
    mgr_file.write_text(mgr_yaml)
    print(f"Generated Manager Manifest: {mgr_file}")

    # 2. Generate Worker Manifests
    for worker_key in config["worker_clusters"].keys():
        worker_yaml = generate_worker_yaml(config, worker_key, templates)
        worker_file = out_dir / f"worker-{worker_key}.yaml"
        worker_file.write_text(worker_yaml)
        print(f"Generated Worker Manifest ({worker_key}): {worker_file}")

    print("\nGeneration Complete! You can inspect the files in k8s/generated/ before applying.")

if __name__ == "__main__":
    main()
