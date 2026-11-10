# Training flywheel (TeamWeave → TrainWeave → DeployWeave)

DeployWeave is the deployment leg of a three-repository flywheel. Arrows marked **[code]** run automatically; **[manual]** steps need a human.

```
 TeamWeave                     TrainWeave                    DeployWeave
 ─────────                     ──────────                    ───────────
 worker_handler invokes each
 step twice, keeps the answer
 with the lower composite risk
        │
        │ [code] dpo_collector writes a chosen/rejected record per step
        ▼
 s3://$DPO_TRAINING_BUCKET/{project}/{team}/{step_id}/{run_id}/dpo_*.json
        │
        │ [manual] someone decides there are enough pairs and invokes the
        │          TrainWeave orchestrator with
        │          dataset_source={"type":"dpo","bucket":…,"prefix":…}
        ▼
                   app.py::_materialise_dpo_dataset          [code]
                     train.jsonl      SFT rows (trained on)
                     train.dpo.jsonl  preference pairs (NOT trained on yet)
                               │
                               │ [code] EC2 spot → bootstrap.sh → train.py
                               ▼
                   s3://$ARTIFACTS_BUCKET/adapters/{job_id}/
                               │
                               │ [code] bootstrap.sh → register_adapter.py
                               │        (skipped when ADAPTER_CATALOG_TABLE unset)
                               ▼
                                          DynamoDB deployweave-adapter-catalog
                                                     │
                                                     │ [code] adapter_resolver
                                                     │        team_provisioner
                                                     ▼
                                          Bedrock agent with the adapter attached
                                                     │
        ┌────────────────────────────────────────────┘
        │ [manual] create/publish the agent alias, then edit team.json in S3:
        │          agents[].bedrock.model_aliases maps model_id → aliasId
        ▼
 TeamWeave's next run uses the new model and produces new pairs
```

## What is still manual

| Step | Why |
|---|---|
| Deciding to train, and invoking the TrainWeave orchestrator | No trigger watches the DPO prefix for a quorum of pairs. |
| Publishing the Bedrock agent alias for the new adapter | `team_provisioner` creates agents; alias publication is separate. |
| Editing `model_aliases` in `team.json` | Team configs live in S3 and are edited by a human. |
| Training on `train.dpo.jsonl` | TrainWeave's trainer is SFT-only today; DPO objective is a deliberate change. |

LoRA note: `lora_validator.py` accepts base models prefixed `amazon.titan-` or `meta.llama`. Other bases may register in the catalog but will not attach at provision time.
