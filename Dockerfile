# Wan2GP Gateway Dedicated Dockerfile
FROM deepbeepmeep/wan2gp:latest

ENV PYTHONUNBUFFERED=1
ENV START_GATEWAY=1

# Compile SageAttention with SM89 and SM120 (RTX 40 and 50 series) support inside dedicated gateway image
RUN rm -rf /tmp/sageattention && \
    git clone https://github.com/thu-ml/SageAttention.git /tmp/sageattention && \
    cd /tmp/sageattention && \
    TORCH_CUDA_ARCH_LIST="8.9;12.0" MAX_JOBS=2 pip install --no-build-isolation --force-reinstall . && \
    rm -rf /tmp/sageattention

WORKDIR /workspace

EXPOSE 50080

CMD ["python3", "wan2gp-gateway/gateway.py"]
