from rich.spinner import SPINNERS
import random

def generate_binary_frames(num_frames=60):
    frames = []
    for _ in range(num_frames):
        val = random.randint(0, 4294967295)
        b = f"{val:032b}"
        f_bin = " ".join([b[i:i+4] for i in range(0, 32, 4)])
        frames.append(f"Processing prompt: [ {f_bin} ]")
    return frames

SPINNERS["sot_robot"] = {
    "interval": 70,
    "frames": generate_binary_frames(60)
}
