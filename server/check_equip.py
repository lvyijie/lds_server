import os

for root, dirs, files in os.walk("04-数据JSON"):
    if "ClothesDatas_dataClass.xml" in files:
        p = os.path.join(root, "ClothesDatas_dataClass.xml")
        with open(p, "r", encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                for eq in ["20001", "20011", "20021", "20041", "20051", "100002", "112031", "113027"]:
                    if f'equipid="{eq}"' in line:
                        print(f"Equipid {eq} -> {line.strip()[:100]}")
