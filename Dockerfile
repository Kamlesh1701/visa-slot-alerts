FROM python:3.11-slim

RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alert.py .
RUN chown -R user:user /app
USER user

ENV PORT=7860 PYTHONUNBUFFERED=1
EXPOSE 7860

CMD ["python", "alert.py"]
