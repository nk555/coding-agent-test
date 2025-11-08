#!/usr/bin/env python3

import asyncio
import argparse
import os
import shlex
import uuid
import yaml
from pathlib import Path
import subprocess

# --- Configuration ---
WORKTREE_DIR = Path(".ob1_worktrees")
AGENT_CONFIG_FILE = "agents.yml"

# --- Helper Functions ---

def get_base_branch():
    """Gets the current git branch."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        print("Error: Could not determine current git branch.")
        print("Please run this script from within a git repository.")
        exit(1)

def load_agent_config(config_file):
    """Loads the agents.yml file."""
    try:
        with open(config_file, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Error: Configuration file '{config_file}' not found.")
        exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing YAML file '{config_file}': {e}")
        exit(1)

# --- MODIFIED FUNCTION ---
async def run_command(command, cwd, agent_id="System", ignore_errors=False):
    """
    Runs a shell command asynchronously and logs its output.
    """
    print(f"   [{agent_id}] Running: {command}")
    
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd
    )

    stdout, stderr = await proc.communicate()

    decoded_stdout = stdout.decode().strip()
    decoded_stderr = stderr.decode().strip()

    # Always print the output for debugging purposes
    if decoded_stdout:
        print(f"   [{agent_id}] STDOUT:")
        print(f"   -----------------")
        print(decoded_stdout)
        print(f"   -----------------")
    if decoded_stderr:
        print(f"   [{agent_id}] STDERR:")
        print(f"   -----------------")
        print(decoded_stderr)
        print(f"   -----------------")

    if proc.returncode != 0 and not ignore_errors:
        print(f"   [{agent_id}] ❌ ERROR (code {proc.returncode})")
        raise subprocess.CalledProcessError(proc.returncode, command, stdout, stderr)
    elif proc.returncode != 0 and ignore_errors:
        print(f"   [{agent_id}] ⚠️ Warning: Command failed (code {proc.returncode}), but errors are ignored.")
    
    return decoded_stdout

# --- Agent Pipeline Steps ---

# --- MODIFIED FUNCTION ---
async def setup_worktree(base_branch, agent_id, task_id):
    """
    Creates a new git worktree and applies current uncommitted changes.
    """
    branch_name = f"ai-agent/{agent_id}-{task_id}"
    worktree_path = WORKTREE_DIR / f"{agent_id}-{task_id}"

    # Clean up any old, failed worktrees first
    if worktree_path.exists():
        print(f"   [{agent_id}] Cleaning up old worktree...")
        await run_command(f"git worktree remove -f {worktree_path}", Path.cwd(), agent_id, ignore_errors=True)
        await run_command(f"git branch -D {branch_name}", Path.cwd(), agent_id, ignore_errors=True)

    # Check for uncommitted changes in the main directory
    status_result = await run_command("git status --porcelain", Path.cwd(), agent_id)
    has_changes = bool(status_result.strip())
    
    if has_changes:
        print(f"   [{agent_id}] Stashing current changes (including untracked files)...")
        await run_command(f"git stash push -u -m ob1-temp-stash-{task_id}-{agent_id}", Path.cwd(), agent_id)
    
    print(f"   [{agent_id}] Setting up worktree at: {worktree_path}")
    WORKTREE_DIR.mkdir(exist_ok=True)
    
    # Create the new worktree from the base branch
    await run_command(
        f"git worktree add -b {branch_name} {worktree_path} {base_branch}",
        Path.cwd(),
        agent_id
    )
    
    # If we stashed changes, apply them to the new worktree
    if has_changes:
        print(f"   [{agent_id}] Applying stashed changes to new worktree...")
        try:
            # Apply the most recent stash (which is ours) to the new worktree
            await run_command("git stash apply stash@{0}", worktree_path, agent_id)
            
            # If apply succeeds, drop the stash from the main repo.
            #print(f"   [{agent_id}] Stash applied successfully. Dropping from main repo.")
            #await run_command("git stash drop stash@{0}", Path.cwd(), agent_id)
        except Exception as e:
            print(f"   [{agent_id}] ❌ ERROR: Failed to apply stashed changes to the worktree.")
            print(f"   [{agent_id}] Your uncommitted changes have been preserved in the stash.")
            print(f"   [{agent_id}] To restore them, run 'git stash pop' in your main terminal.")
            print(f"   [{agent_id}] The agent will continue on a clean branch without your changes.")
            # IMPORTANT: We do NOT drop the stash if the apply fails.

    return worktree_path, branch_name


async def run_agent_task(worktree_path, agent_command, prompt, agent_id):
    """Runs the agent's specific command inside its worktree."""
    print(f"   [{agent_id}] Running AI task...")
    
    # Inject variables into the command string
    final_command = agent_command.format(
        prompt=shlex.quote(prompt),
        worktree_path=shlex.quote(str(worktree_path))
    )
    
    await run_command(final_command, worktree_path, agent_id)

async def commit_and_push_changes(worktree_path, branch_name, prompt, agent_id):
    """Commits and pushes the agent's changes."""
    print(f"   [{agent_id}] Committing and pushing changes...")
    
    # Check if there are any changes
    try:
        await run_command("git diff-index --quiet HEAD", worktree_path, agent_id)
        print(f"   [{agent_id}] No changes detected. Skipping PR.")
        return False  # No changes
    except subprocess.CalledProcessError:
        print(f"   [{agent_id}] Changes detected. Proceeding...")
        # This error means changes WERE found, which is what we want.
        pass

    await run_command("git add .", worktree_path, agent_id)
    await run_command(
        f"git commit -m 'feat: AI ({agent_id}) - {prompt[:50]}...'", 
        worktree_path, 
        agent_id
    )
    await run_command(
        f"git push origin {branch_name}", 
        worktree_path, 
        agent_id
    )
    return True # Changes were pushed

async def create_pull_request(worktree_path, base_branch, branch_name, prompt, agent_id):
    """Creates a GitHub Pull Request using the 'gh' CLI."""
    print(f"   [{agent_id}] Creating Pull Request...")
    
    title = f"AI Agent ({agent_id}): {prompt}"
    body = f"This PR was generated by the `ob1` orchestrator.\n\n**Agent:** `{agent_id}`\n**Task:** `{prompt}`"
    
    await run_command(
        f"gh pr create --base {base_branch} --head {branch_name} --title \"{title}\" --body \"{body}\"",
        worktree_path,
        agent_id
    )

async def cleanup_worktree(worktree_path, branch_name, agent_id):
    """Removes the git worktree and optionally the remote branch."""
    print(f"   [{agent_id}] Cleaning up worktree...")
    
    # Run from the main repo root
    # await run_command(f"git worktree remove -f {worktree_path}", Path.cwd(), agent_id, ignore_errors=True)
    
# --- Main Orchestrator ---

async def run_agent_pipeline(agent_config, prompt, base_branch, task_id):
    """
    The full, end-to-end pipeline for a single agent.
    """
    agent_id = agent_config['name']
    agent_command = agent_config['command']
    print(f"🚀 Starting pipeline for agent: {agent_id} (Task: {task_id})")
    
    worktree_path = None
    branch_name = None
    
    try:
        worktree_path, branch_name = await setup_worktree(
            base_branch, agent_id, task_id
        )
        
        await run_agent_task(worktree_path, agent_command, prompt, agent_id)
        
        changes_pushed = await commit_and_push_changes(
            worktree_path, branch_name, prompt, agent_id
        )
        
        if changes_pushed:
            await create_pull_request(
                worktree_path, base_branch, branch_name, prompt, agent_id
            )
        
        print(f"✅ Pipeline for {agent_id} COMPLETED successfully.")
        return f"Success: {agent_id}"

    except Exception as e:
        print(f"❌ Pipeline for {agent_id} FAILED: {e}")
        return f"Failure: {agent_id}"
    
    finally:
        if worktree_path and worktree_path.exists():
            await cleanup_worktree(worktree_path, branch_name, agent_id)

# --- Entrypoint ---

async def main():
    parser = argparse.ArgumentParser(description="Run k AI agents on a repo.")
    parser.add_argument("-m", "--prompt", required=True, help="The task prompt for the AI agents.")
    parser.add_argument("-k", type=int, default=1, help="The number of agents to run in parallel.")
    args = parser.parse_args()

    # --- Setup ---
    base_branch = get_base_branch()
    task_id = str(uuid.uuid4())[:6] # Unique ID for this run
    
    print(f"Starting Orchestrator (Run ID: {task_id})")
    print(f"   Repo: {Path.cwd().name}")
    print(f"   Base Branch: {base_branch}")
    print(f"   Task: \"{args.prompt}\"")
    print(f"   Agents (k): {args.k}\n")

    # --- Load Agents ---
    config = load_agent_config(AGENT_CONFIG_FILE)
    all_agents = config.get('agents', [])
    
    if not all_agents:
        print("Error: No agents found in 'agents.yml'.")
        return

    agents_to_run = []
    if args.k > 0 and all_agents:
        for i in range(args.k):
            # Cycle through the available agents
            agent_template = all_agents[i % len(all_agents)]
            
            # Create a copy of the agent config to modify it
            new_agent_config = agent_template.copy()
            
            # Give the new agent a unique name for this run to avoid conflicts
            # This is important for worktree and branch names
            new_agent_config['name'] = f"{new_agent_config['name']}-{i+1}"
            
            agents_to_run.append(new_agent_config)

    if not agents_to_run:
        print("Warning: No agents selected to run (k=0 or no agents in config).")
        
    print(f"Selected agents: {[a['name'] for a in agents_to_run]}")
    print("---")

    # --- Create and Run Tasks Concurrently ---
    tasks = []
    for agent_config in agents_to_run:
        tasks.append(
            run_agent_pipeline(agent_config, args.prompt, base_branch, task_id)
        )
    
    results = await asyncio.gather(*tasks)
    
    print("\n--- Run Summary ---")
    for res in results:
        print(res)

if __name__ == "__main__":
    # Ensure GH CLI is installed and authenticated
    try:
        subprocess.run(["gh", "--help"], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: 'gh' (GitHub CLI) is not installed or not authenticated.")
        print("Please install it.")
        exit(1)

    asyncio.run(main())