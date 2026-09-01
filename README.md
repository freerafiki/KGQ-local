# KGQ-local
Knowledge Graph Query framework for local experiments

## Run the fastAPI backend
```
uvicorn app:app --host 0.0.0.0        # reachable from the LAN; prints your IPs at startup
```

## Run the static frontend
```
./run_frontend.sh                     # page on http://localhost:8080, prints your IPs
```

## Access from other machines (lab / same Wi-Fi)
Visitors open `http://<your-lan-ip>:8080` in their browser (find it with `hostname -I`).
The page automatically targets the API on the same host at port 8000.
If a firewall is active, allow ports 8000 and 8080 (`sudo ufw allow 8000 && sudo ufw allow 8080`). 


## TODO: Experiments 
Check results with:
- query
- rewritten query
- rewritten query with specific field 
- translated query 
- query + keywords
- embedded query expanded
- with reranking