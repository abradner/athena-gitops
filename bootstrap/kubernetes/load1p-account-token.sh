#!/bin/bash

# Create the namespace
kubectl create namespace external-secrets

# Inject the Service Account Token
kubectl create secret generic op-token -n external-secrets --from-literal=token="<YOUR_SERVICE_ACCOUNT_TOKEN>"
