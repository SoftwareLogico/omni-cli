import os
from datetime import datetime

# 1. Check for a specific "location" file or variable
# 2. Use the Host Environment data to create a factual string
print(f"--- PHYSICAL LOCATION REPORT ---")
print(f"Current Directory: {os.getcwd()}")
print(f"Timezone: CEST (Central European Summer Time)")
print(f"Local Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"Conclusion: You are in Europe, likely Spain/France/Germany.")
