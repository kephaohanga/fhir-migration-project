import requests

url = "https://hapi.fhir.org/baseR4/Patient?_count=3"
response = requests.get(url, headers={"Accept": "application/fhir+json"}, timeout=30)

print("Status code:", response.status_code)   # 200 means success
data = response.json()
for entry in data.get("entry", []):
    patient = entry["resource"]
    print(patient["id"], patient.get("gender"), patient.get("birthDate"))