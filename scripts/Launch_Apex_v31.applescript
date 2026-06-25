-- Launch Apex v31.1.0 — one-click cold boot via native App Bundle (no Terminal).
on run
	set apexRoot to "/Users/chrisgordon/Projects/IG-Agent-v31-Sandbox"
	set launchCmd to "export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin; " & ¬
		"pkill -f daemon_supervisor.sh 2>/dev/null || true; " & ¬
		"pkill -f ig_agent 2>/dev/null || true; " & ¬
		"sleep 2; " & ¬
		"cd " & quoted form of apexRoot & " || exit 1; " & ¬
		"mkdir -p src/data/v31-production/logs; " & ¬
		"rm -f src/data/.ig_agent_v29.lock src/data/.ig_agent_v30_port_8080.lock src/data/v31-production/supervisor.pid 2>/dev/null || true; " & ¬
		"if lsof -tiTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then lsof -tiTCP:8080 -sTCP:LISTEN | xargs kill -TERM 2>/dev/null || true; sleep 2; fi; " & ¬
		"if lsof -tiTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then exit 2; fi; " & ¬
		"DAEMON_SUPERVISOR_REDIRECT=1 nohup ./scripts/daemon_supervisor.sh >> src/data/v31-production/logs/supervisor.log 2>&1 & " & ¬
		"sleep 1; " & ¬
		"open 'http://127.0.0.1:8080/'"
	try
		do shell script launchCmd
	on error errMsg number errNum
		display dialog "Launch Apex v31 failed (" & errNum & "): " & errMsg buttons {"OK"} default button 1 with icon caution
	end try
end run
