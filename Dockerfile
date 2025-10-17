# Base image
FROM python:3.11-slim

# Set working directory (same as your app folder)
WORKDIR /app

# Copy dependency file first for build caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files into the container
COPY . .

# ✅ Ensure the SQLite database folder exists and is writable
RUN mkdir -p /app/data && chmod 777 /app/data

# Expose the port Hugging Face expects
EXPOSE 7860

# Set default environment variables for Hugging Face Spaces
ENV PORT=7860
ENV OPENAI_API_KEY=""
ENV OPENAI_BASE_URL="https://aipipe.org/openai/v1"
ENV AIMODEL_NAME="gpt-4o"
ENV GITHUB_TOKEN=""
ENV GITHUB_OWNER=""
ENV STUDENT_SECRET=""
ENV DB_PATH="sqlite:///./data/tds_deployer.sqlite"

# Run FastAPI app using Uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
