import sounddevice as sd
import numpy as np

# Generate 440Hz sine wave
sample_rate = 24000
t = np.linspace(0, 1, sample_rate, endpoint=False)
wave = 0.5 * np.sin(2 * np.pi * 440 * t)

# Play as float32 array
print("Playing float32 array...")
sd.play(wave.astype(np.float32), sample_rate)
sd.wait()

# Play as list
print("Playing list...")
sd.play(wave.tolist(), sample_rate)
sd.wait()
