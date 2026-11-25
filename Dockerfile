# Small official Python image
FROM python:3.12-slim
WORKDIR /app

# Install libraries first (cached between builds if requirements.txt is unchanged)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app code (secrets come from --env-file at run time, never baked in)
COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
