import os
import subprocess
import sys
from pathlib import Path

def run_cmd(args, check=True):
    print(f"Running: {' '.join(args)}")
    result = subprocess.run(args, capture_output=True, text=True, check=check)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    return result

def setup_git_and_keys():
    email = "s.md.saifulhuq007@gmail.com"
    username = "saifulhuq01"
    
    ssh_dir = Path("C:/Users/smdsa/.ssh")
    ssh_dir.mkdir(parents=True, exist_ok=True)
    ssh_key_path = ssh_dir / "id_ed25519"
    
    print("\n=== STEP 1: Creating SSH Key ===")
    if ssh_key_path.exists():
        print(f"SSH Key already exists at {ssh_key_path}. Skipping generation.")
    else:
        # Generate SSH key using subprocess (args list prevents escaping errors)
        run_cmd([
            "ssh-keygen",
            "-t", "ed25519",
            "-C", email,
            "-f", str(ssh_key_path),
            "-N", ""
        ])
        print("SSH Key generated successfully!")
    
    print("\n=== STEP 2: Configuring Git User Details ===")
    git_path = "C:/Program Files/Git/cmd/git.exe"
    run_cmd([git_path, "config", "--local", "user.name", username])
    run_cmd([git_path, "config", "--local", "user.email", email])
    print("Git user.name and user.email set locally.")

    print("\n=== STEP 3: Creating GPG Key ===")
    gpg_path = "C:/Program Files/Git/usr/bin/gpg.exe"
    
    # Check if a GPG key for the email already exists
    check_gpg = subprocess.run([gpg_path, "--list-keys", email], capture_output=True, text=True)
    if check_gpg.returncode == 0:
        print(f"GPG key for {email} already exists. Using existing key.")
    else:
        # Write temporary GPG batch config
        batch_config = (
            "Key-Type: EDDSA\n"
            "Key-Curve: ed25519\n"
            "Key-Usage: sign\n"
            "Subkey-Type: ECDH\n"
            "Subkey-Curve: cv25519\n"
            "Subkey-Usage: encrypt\n"
            "Name-Real: saifulhuq01\n"
            "Name-Email: s.md.saifulhuq007@gmail.com\n"
            "Expire-Date: 0\n"
            "%no-protection\n"
            "%commit\n"
        )
        batch_file = Path("gpg_params.txt")
        batch_file.write_text(batch_config)
        
        try:
            print("Generating GPG key in batch mode...")
            run_cmd([gpg_path, "--batch", "--generate-key", "gpg_params.txt"])
            print("GPG Key generated successfully!")
        finally:
            if batch_file.exists():
                batch_file.unlink()
                
    print("\n=== STEP 4: Configuring Git Commit Signing ===")
    # Extract the GPG key ID
    list_keys = subprocess.run([
        gpg_path, "--list-secret-keys", "--keyid-format", "LONG", email
    ], capture_output=True, text=True, check=True)
    
    key_id = None
    for line in list_keys.stdout.splitlines():
        if "sec " in line or "sec  " in line:
            # Format is usually 'sec   ed25519/KEY_ID_HERE 2026-06-10 [SC]'
            parts = line.split()
            for part in parts:
                if "/" in part:
                    key_id = part.split("/")[1]
                    break
            if key_id:
                break
                
    if not key_id:
        print("Error: Could not extract generated GPG Key ID from list-keys output.", file=sys.stderr)
        sys.exit(1)
        
    print(f"Found GPG Key ID: {key_id}")
    
    # Configure Git to use this signing key
    run_cmd([git_path, "config", "--local", "user.signingkey", key_id])
    run_cmd([git_path, "config", "--local", "commit.gpgsign", "true"])
    run_cmd([git_path, "config", "--local", "gpg.program", gpg_path])
    print("Git configured to sign commits with GPG key!")
    
    print("\n=== STEP 5: Exporting Public Keys for GitHub ===")
    print("\n--- SSH Public Key (Add this to GitHub Settings -> SSH and GPG keys -> New SSH Key) ---")
    pub_ssh = Path(str(ssh_key_path) + ".pub").read_text().strip()
    print(pub_ssh)
    
    print("\n--- GPG Public Key (Add this to GitHub Settings -> SSH and GPG keys -> New GPG Key) ---")
    gpg_export = subprocess.run([gpg_path, "--armor", "--export", email], capture_output=True, text=True, check=True)
    print(gpg_export.stdout.strip())
    
    print("\n==============================================")
    print("Git SSH and GPG keys setup completed successfully!")
    print("Please add the keys above to your GitHub profile.")
    print("==============================================")

if __name__ == "__main__":
    setup_git_and_keys()
