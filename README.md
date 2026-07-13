# Devtron Automation Workspace

Declarative application management for Devtron using the `devtron-cli`.


## Prerequisites

Before using the CLI, ensure the following resources already exist in Devtron under **Global Configurations**:

- **Project**
- **Git Account**
- **Container Registry**

---

# Installation

Install the Devtron CLI using pip:

```bash
pip3 install devtron-cli
```

Verify the installation:

```bash
tron --help
```

---

# Generate an API Access Token

The CLI authenticates using a Devtron API token.

## Steps

1. Log in to the Devtron UI.
2. Click your **Profile** (top-right corner).
3. Navigate to **API Tokens**.
4. Click **Generate Token**.
5. Provide:
   - **Name** (for example: `devtron-cli`)
   - **Expiration** (recommended)
6. Click **Generate**.
7. Copy the generated token immediately.
   > **Note:** The token is displayed only once.

---

# Configure Authentication

Export your Devtron URL and API token before running any commands.

```bash
export DEVTRON_URL="https://devtron.test.paywithring.com"
export DEVTRON_API_TOKEN="<YOUR_API_TOKEN>"
```

You can verify authentication by running:

```bash
tron version
```

or

```bash
tron --help
```

---

# Usage

Run all commands from the repository root.

## Create a New Application

Creates the application and configures the following:

- Git repository
- Build pipeline
- Deployment pipeline
- Secrets
- Environment configuration

```bash
tron --config example/new_admin-app.yaml create-app
```

---

## Update an Existing Application

Applies configuration changes to an existing Devtron application.

```bash
tron --config example/new_admin-app.yaml update-app
```

---

# Configuration Files

| File | Description |
|------|-------------|
| `config.yaml` | Blank configuration template |
| `example/new_admin-app.yaml` | Main application configuration |
| `example/base-admin-service-cli-values.yaml` | Base deployment values |
| `example/override-admin-service-cli-dev-pe-values.yaml` | Environment-specific overrides |

---

# Quick Start

1. Install the CLI.

```bash
pip3 install devtron-cli
```

2. Generate an API token from Devtron.

3. Export authentication variables.

```bash
export DEVTRON_URL="https://devtron.test.paywithring.com"
export DEVTRON_API_TOKEN="<YOUR_API_TOKEN>"
```

4. Create the application.

```bash
tron --config example/new_admin-app.yaml create-app
```

5. Update the application after making changes.

```bash
tron --config example/new_admin-app.yaml update-app
```

---