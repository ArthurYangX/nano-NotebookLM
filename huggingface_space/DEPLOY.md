# Deploy nano-NotebookLM to HuggingFace Spaces

End result: a public URL like
`https://huggingface.co/spaces/<your-username>/nano-notebooklm` that
anyone can visit and use as a live demo.

## One-time setup

1. **Create a HuggingFace account** (free): <https://huggingface.co/join>

2. **Install the HF CLI and log in**:

   ```bash
   pip install -U "huggingface_hub[cli]"
   huggingface-cli login
   ```

   Generate a write-scoped token at <https://huggingface.co/settings/tokens>.

3. **Create a new Space**:
   <https://huggingface.co/new-space>
   - Owner: yourself (or an org)
   - Space name: `nano-notebooklm` (or anything you like)
   - License: Apache 2.0
   - SDK: **Docker** → "Blank" template
   - Visibility: Public (or Private — both work)
   - Hardware: **CPU basic (free)** — works fine for this project

4. **(optional but recommended) Add a default LLM key as a Space secret**:

   On the Space page → *Settings* → *Variables and secrets* → *New secret*.
   Add three secrets so the demo works out-of-the-box without users
   having to fill in their own keys:

   | Name              | Example value                                            |
   | ----------------- | -------------------------------------------------------- |
   | `OPENAI_API_KEY`  | `sk-...`                                                 |
   | `OPENAI_BASE_URL` | `https://api.openai.com/v1` (or any compatible endpoint) |
   | `OPENAI_MODEL`    | `gpt-4o-mini` / `deepseek-v4-pro` / `gemini-2.0-flash`   |

   Without this, the first visitor sees an empty Settings page and has
   to paste their own key — fine for a demo you control, awkward for a
   public showcase.

## Deploy (and redeploy)

From the project root:

```bash
./huggingface_space/deploy.sh <your-username>/<space-name>
```

Example:

```bash
./huggingface_space/deploy.sh ArthurYangX/nano-notebooklm
```

The script:

1. Stages a clean copy of the repo into a temp directory
2. Swaps in `huggingface_space/README.md` (the one with the YAML
   frontmatter HF Spaces requires) as the root `README.md`
3. Initializes a fresh git repo and force-pushes to the Space

After pushing, HF will build the docker image (first build ~5-10 min,
subsequent ones ~2 min thanks to layer cache). When the *Building*
badge turns green, the Space is live.

## Updating the demo

Make changes on `main` of the GitHub repo, then re-run the deploy
script. The push is force-style on a fresh tree, so there's no
divergence to manage — the Space always mirrors whatever's on disk.

## Troubleshooting

- **Build fails with `502 Bad Gateway` from `deb.debian.org`** —
  intermittent Debian mirror flake; click *Factory rebuild* on the
  Space page once and it usually passes the second time.

- **First-page load shows "no providers configured"** — you skipped
  step 4. Either add the Space secrets, or instruct users to add a
  provider via the Settings UI.

- **Uploads disappear after the Space sleeps** — expected on free
  tier (ephemeral storage). Upgrade to a paid tier with persistent
  storage to keep `./artifacts` between sleeps.
