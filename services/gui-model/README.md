# GUI model service

This service is a pinned, loopback-only vLLM deployment of GUI-Owl 1.5. The
model server itself performs inference only: it does not call ADB, control
Windows, contact the cloud provider, or own chat persistence.

The Windows control console now owns the complete Android orchestration path:

1. persistent local or cloud-backed chat sessions;
2. cloud-generated user reply and normalized high-level execution goal;
3. fresh Android screenshot acquisition through the configured MuMu ADB;
4. a loopback OpenAI-compatible multimodal request to this service;
5. strict parsing of one official `mobile_use` tool-call envelope;
6. cancellation, target-serial validation, and eight narrow hard-stop classes;
7. one restricted atomic ADB action;
8. a fresh post-action screenshot and a persistence-safe timeline event.

Direct local chat calls this same endpoint with text only and never activates
the Android automation Adapter. Device screenshots are refused if the GUI model
endpoint is not syntactically loopback, and earlier screenshots are compressed
to text-only action history rather than resent.

Runtime versions and model revisions are installed by
`scripts/wsl/bootstrap-gui-model.sh` and recorded by the generated
`/srv/ai-game/run/environment-versions.txt` file.
