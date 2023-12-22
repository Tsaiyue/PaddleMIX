# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
export FLAGS_eager_delete_tensor_gb=0.0
export FLAGS_fraction_of_gpu_memory_to_use=0.98
export FLAGS_conv_workspace_size_limit=4096
export FLAGS_set_to_1d=0
export FLAG_RECOMPUTE=1

export OUTPUT_DIR="fp32_1gpu_gb16"
export BATCH_SIZE=16
export MAX_ITER=100000

LOG_DIR=${OUTPUT_DIR}/log
rm -rf ${LOG_DIR}
rm -rf ${OUTPUT_DIR}

mkdir -p ${LOG_DIR}

nohup python -u -m paddle.distributed.launch --log_dir ${LOG_DIR} --gpus "0" train_lcm.py \
    --do_train \
    --output_dir ${OUTPUT_DIR} \
    --per_device_train_batch_size ${BATCH_SIZE} \
    --gradient_accumulation_steps 1 \
    --learning_rate 1e-6 \
    --weight_decay 0.0 \
    --max_steps ${MAX_ITER} \
    --lr_scheduler_type "constant" \
    --warmup_steps 0 \
    --image_logging_steps 400 \
    --logging_steps 10 \
    --resolution 512 \
    --save_steps 2000 \
    --save_total_limit 20 \
    --seed 23 \
    --dataloader_num_workers 4 \
    --pretrained_model_name_or_path runwayml/stable-diffusion-v1-5 \
    --file_list ./data/filelist/laion_aes.filelist.list \
    --model_max_length 77 \
    --max_grad_norm 1 \
    --disable_tqdm True \
    --overwrite_output_dir \
    --loss_type "huber" \
    --lora_rank 64 \
    --is_lora True > ${OUTPUT_DIR}/running.log 2>&1 &