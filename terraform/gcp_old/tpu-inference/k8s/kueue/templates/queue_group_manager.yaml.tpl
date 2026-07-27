---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: ${PROFILE_NAME}
spec:
  nodeLabels:
    tpu-ci.google.com/profile: ${PROFILE_NAME}${EXTRA_NODE_LABELS}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: ${PROFILE_NAME}
spec:
  cohortName: ${COHORT_NAME}
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: ${NAMESPACE}
  admissionChecksStrategy:
    admissionChecks:
      - name: ${COHORT_NAME}-multikueue-dispatch
  resourceGroups:
    - coveredResources:
        - google.com/tpu
        # cpu and memory are included in coveredResources so Kueue admits pods whose
        # initContainers or helper containers (e.g., buildkite bootstrap/imagecheck) request CPU/memory.
        - cpu
        - memory
      flavors:
        - name: ${PROFILE_NAME}
          resources:
            - name: google.com/tpu
              nominalQuota: ${NOMINAL_QUOTA}
            # Unconstrained quotas for helper CPU and memory to ensure TPU quota remains the sole scheduling constraint
            - name: cpu
              nominalQuota: "10000"
            - name: memory
              nominalQuota: 10000Gi
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: ${PROFILE_NAME}
  namespace: ${NAMESPACE}
spec:
  clusterQueue: ${PROFILE_NAME}
