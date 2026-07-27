---
apiVersion: kueue.x-k8s.io/v1beta2
kind: MultiKueueConfig
metadata:
  name: ${COHORT_NAME}-workers
spec:
  clusters:
${WORKER_LIST}
