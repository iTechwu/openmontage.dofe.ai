# Deterministic Video Smoke Compose Director

This pipeline is an infrastructure acceptance test, not a creative production path.

The OpenMontage worker renders this stage locally with ffmpeg. It must not call a
model, fetch external media, or substitute another provider. The output is a short
H.264/yuv420p MP4 with a visible title and motion marker. The worker validates the
file with ffprobe, writes the canonical `render_report`, and lets the normal Job
artifact bridge publish the final video.

Use this pipeline only for deterministic service health and AgentSpace integration
verification. Use the normal production pipelines for user-facing creative work.
