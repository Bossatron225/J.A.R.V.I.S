# Local voice clone

Makes Jarvis speak with his current ElevenLabs voice, fully offline, after a one-time setup.

1. **Capture the current voice** (local, uses your existing ElevenLabs config, costs ElevenLabs credits):
   ```
   python scripts/generate_voice_reference_dataset.py --yes
   ```
   Produces `voice_clone/dataset/jarvis_voice_reference.zip`.

2. **Train the conversion model** (one-time, free Colab GPU — the only step that touches an
   outside connection): open `voice_clone/train_rvc_colab.ipynb` in Google Colab, run the
   cells in order, upload the zip from step 1 when prompted. Follow the in-notebook
   instructions for the training UI. Download the resulting `jarvis.pth` and `jarvis.index`.

3. **Install locally and switch on**:
   ```
   pip install -r requirements.txt
   ```
   Place `jarvis.pth` and `jarvis.index` in `voice_clone/rvc_models/`, then set
   `"tts_engine": "cloned_voice"` in `config/api_keys.json`.

From then on, every reply runs entirely on this Mac — no ElevenLabs calls.

**To go back to the cloud voice at any time:** set `"tts_engine": "elevenlabs"` again in
`config/api_keys.json`. Nothing else needs to change — both engines coexist.
