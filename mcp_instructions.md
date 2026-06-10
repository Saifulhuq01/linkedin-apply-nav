# Instructions for LinkedIn MCP Integration

We have already installed `uv` and successfully registered the LinkedIn MCP server inside your Claude Desktop configuration file!

Follow these quick steps to authenticate LinkedIn and start searching/scoring jobs.

---

### Step 1: Authenticate with LinkedIn (One-time Setup)

Since LinkedIn requires resolving CAPTCHAs and completing Multi-Factor Authentication (MFA), you must open the browser session manually once to log in.

1. Open **PowerShell** on your machine.
2. Copy and paste the following commands to start the interactive login browser:
   ```powershell
   $env:Path = "C:\Users\smdsa\.local\bin;$env:Path"
   cd c:\Users\smdsa\Desktop\automation\linkedin-mcp-server
   uv run -m linkedin_mcp_server --login
   ```
3. A browser window controlled by Patchright will pop up.
4. **Log into LinkedIn** as you normally would.
5. Solve any MFA or CAPTCHA prompts that appear.
6. Once you are successfully logged in and viewing your home feed, you can close the browser window. The session will be saved persistently at `~/.linkedin-mcp/profile`.

---

### Step 2: Launch or Restart Claude Desktop

1. If Claude Desktop is already open, **completely close it** (make sure to quit it from the Windows system tray / notification area if it runs in the background).
2. Open **Claude Desktop**.
3. Look at the bottom-right corner of the prompt box to see if the **LinkedIn MCP server** (represented by a hammer/tool icon or connection indicator) is successfully connected.

---

### Step 3: Run the Job Matching and Scoring Workflow

Now you can chat with Claude in Claude Desktop to perform search, fetch details, and score them against your resume.

Here is a copy-pasteable prompt you can use:

> **Prompt to copy into Claude Desktop:**
>
> "Search for Java Spring Boot Kafka jobs in Bangalore or Chennai, past week, mid-senior level. After listing the jobs, retrieve details for the top 5 most relevant jobs.
>
> Here is my resume:
> [Paste the content of Mohammed_Saifulhuq_Resume.txt here]
>
> Please score each job on a scale of 0-100 based on my resume, highlight the matching skills, point out any skill gaps, and write a custom application cover note / message for the top-scoring job."

---

### Understanding the MCP Server Tools Available to Claude:
- `search_jobs` — Searches for jobs by keyword, location, date posted, experience level, etc.
- `get_job_details` — Fetches the full text/description of a job posting.
- `get_my_profile` — Fetches your own profile information.
- `get_person_profile` — Fetches any person's public profile information.
- `send_message` — Sends a message to connections.
