# Cloud eval runbook (ADR-019 reranker)

Run the real-repo retrieval eval on a **spot NVIDIA T4** in Google Cloud instead of
tying up a local machine for hours. The reranker (arm C) — the whole bottleneck — runs
on the GPU, so a job that takes ~2–3 h on a CPU box finishes in **minutes**.

**Design (Option A):** a self-deleting spot VM. It pulls a bundle from a GCS bucket, runs
the eval, uploads results back, and deletes itself. Nothing runs between jobs, so idle
cost is ~a few cents/month (just the bucket). See `startup.sh` + `launch_eval.ps1`.

- **Project:** `prefabinventoryapp` · **Region/zone:** `us-central1` / `us-central1-a`
- **Cost:** ~pennies per run on spot; **the whole eval program is a few dollars.** The
  only way it gets expensive is an orphaned VM — designed out three ways (self-delete +
  hard 3 h `--max-run-duration=DELETE` + a budget alert).

---

## Prerequisites (once per machine)

Install the Google Cloud CLI, then sign in and select the project:

```powershell
# Install: https://cloud.google.com/sdk/docs/install  (Windows installer)
gcloud auth login
gcloud config set project prefabinventoryapp
```

---

## One-time setup

### 1. Enable the APIs

```powershell
gcloud services enable compute.googleapis.com storage.googleapis.com
```

### 2. Request GPU quota  ← **the long pole; do this first**

Fresh projects have **zero** GPU quota. Approval takes minutes to a couple of days, so
submit it before anything else.

1. Console → **IAM & Admin → Quotas & System Limits**.
2. Filter for **`Preemptible NVIDIA T4 GPUs`**, region **`us-central1`** (spot = preemptible).
3. Select it → **Edit Quota** → request **1** → submit.

Until this is granted, the VM-create step fails with a quota error. That's expected.

### 3. Create the GCS bucket (inputs + outputs)

```powershell
gcloud storage buckets create gs://prefabinventoryapp-code-eval `
  --location=us-central1 --uniform-bucket-level-access
```

### 4. Create the service account the VM runs as

It needs two powers: read/write the bucket, and delete itself.

```powershell
# create it
gcloud iam service-accounts create eval-runner --display-name="ADR-019 eval runner"

# read/write ONLY this bucket
gcloud storage buckets add-iam-policy-binding gs://prefabinventoryapp-code-eval `
  --member="serviceAccount:eval-runner@prefabinventoryapp.iam.gserviceaccount.com" `
  --role=roles/storage.objectAdmin

# allow the VM to delete itself when the job finishes
gcloud projects add-iam-policy-binding prefabinventoryapp `
  --member="serviceAccount:eval-runner@prefabinventoryapp.iam.gserviceaccount.com" `
  --role=roles/compute.instanceAdmin.v1

# let YOU launch a VM that runs as this service account
gcloud iam service-accounts add-iam-policy-binding `
  eval-runner@prefabinventoryapp.iam.gserviceaccount.com `
  --member="user:edb@eganco.com" --role=roles/iam.serviceAccountUser
```

*(`compute.instanceAdmin.v1` is broad; it can be tightened to a custom role with only
`compute.instances.delete` later. Fine as-is for a single-purpose runner.)*

### 5. Budget alert (the cheap tripwire)

Console → **Billing → Budgets & alerts → Create budget**: scope to project
`prefabinventoryapp`, amount **$10**, alert at 50/90/100%. This emails you if anything
ever runs away — belt to the self-delete's suspenders.

---

## Running an eval

From the **repo root** (so the bundle paths resolve):

```powershell
.\cloud\launch_eval.ps1
```

That tars the bundle, uploads it, and launches the VM. It prints the three commands you
need next. The VM **self-deletes when done (~30–45 min)**, including driver install +
`pip install` + the GPU run.

**Watch it live** (startup-script output streams to the serial console):

```powershell
gcloud compute instances get-serial-port-output adr019-eval-<stamp> --zone=us-central1-a `
  | Select-String 'startup|device|/C:|verdict|reranker'
```

You want to see `device=cuda` on the arm lines — that's the proof the GPU is actually
doing the reranking.

**When the VM is gone, fetch the results:**

```powershell
# the authoritative baseline (all 5 repos, one process)
gcloud storage cp gs://prefabinventoryapp-code-eval/outputs/result.jsonl `
  .\benchmarks\real_repo\real_repo_authoritative.jsonl

# the full run log — contains verdict()'s own printed pooled C-B scorecard
gcloud storage cp gs://prefabinventoryapp-code-eval/outputs/eval-run.log .
```

**Confirm nothing is left running** (should be empty):

```powershell
gcloud compute instances list --project=prefabinventoryapp
```

---

## Changing the job (e.g. the clause-3 private slice)

The default job is `--arms B,C` over all prepared repos (the authoritative reranker run).
To run something else, drop a one-line args file in the bucket before launching:

```powershell
# example: only the private slice, all four arms
"--arms A,B,C,D --repos privateslice" | Out-File -Encoding ascii eval-args.txt
gcloud storage cp eval-args.txt gs://prefabinventoryapp-code-eval/inputs/eval-args.txt
.\cloud\launch_eval.ps1
```

`startup.sh` reads `inputs/eval-args.txt` if present, else defaults to `--arms B,C`.
(Delete the file to go back to the default.)

---

## Safety / cost notes

- **Idle cost ≈ the bucket only** (a few cents/month). Delete the bucket to zero it out.
- **The VM cannot outlive its job:** it self-deletes on completion, a preempted spot VM
  is DELETED (not left stopped), and `--max-run-duration=10800s` DELETES it after 3 h no
  matter what. If you ever see one in `instances list` that you didn't just launch,
  delete it: `gcloud compute instances delete NAME --zone=us-central1-a`.
- **Spot preemption** (rare over a ~30 min job): the VM just disappears with no results.
  Re-run `.\cloud\launch_eval.ps1` — the bundle is already in the bucket.
- **First-run failures** are almost always (a) GPU quota not granted yet [step 2], or
  (b) the DLVM image family name drifted — list current ones with:
  `gcloud compute images list --project=deeplearning-platform-release --filter="family~debian-12"`
  and update `--image-family` in `launch_eval.ps1`.
