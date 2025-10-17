---
title: TDS Project 1 Deployer
emoji: 🚀
colorFrom: blue
colorTo: indigo
sdk: docker
app_file: Dockerfile
pinned: true
---

# 🚀 **TDS Project 1 Deployer**

[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Hugging Face](https://img.shields.io/badge/Hosted%20on-HuggingFace-yellow?logo=huggingface)](https://huggingface.co/spaces/VinaySinghChaudhary/tds_project1_vinay)
[![GitHub](https://img.shields.io/badge/Integration-GitHub-blue?logo=github)](https://github.com/VinaySinghChaudhary)
[![Python](https://img.shields.io/badge/Python-3.11+-green?logo=python)](https://www.python.org/)
[![Status](https://img.shields.io/badge/Status-Online-success?logo=vercel)](https://vinaysinghchaudhary-tds_project1_vinay.hf.space/health)

---

## 🧠 **Overview**

**TDS Project 1 Deployer** is a FastAPI-based automation service hosted on **Hugging Face Spaces (Docker)** that:
- Accepts **Round 1 & Round 2 JSON tasks** from the **TDS Server**,
- Automatically creates **GitHub repositories** per task,
- Pushes generated web app files to GitHub,
- Enables **GitHub Pages deployment**, and
- Sends evaluation results back via webhook.

It’s the backend bridge between **TDS AI evaluation tasks** and **automated GitHub deployment**.

---

## ⚙️ **Tech Stack**

| Layer | Technology |
|-------|-------------|
| Backend | [FastAPI](https://fastapi.tiangolo.com/) |
| Hosting | [Hugging Face Spaces (Docker)](https://huggingface.co/spaces) |
| LLM | OpenAI (configurable, e.g., `gpt-4o` or `gpt-5-nano`) |
| Database | SQLite (via SQLModel + SQLAlchemy) |
| SCM | GitHub API (automated repo + Pages deployment) |

---

## 🧩 **Key Endpoints**

### 🔹 `GET /health`

Check service liveness.  
```bash
curl https://vinaysinghchaudhary-tds_project1_vinay.hf.space/health
