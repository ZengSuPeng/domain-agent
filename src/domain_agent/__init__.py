def main():
    import uvicorn
    uvicorn.run("domain_agent.main:app", host="127.0.0.1", port=8000, reload=True)