import torch
import torch.nn.functional as F
from transformers import AutoModelForSequenceClassification
from peft import LoraConfig, get_peft_model, TaskType, prepare_model_for_kbit_training

class ClassificationModel:
    def __init__(self, model_checkpoint, num_labels, quantized_model=False, lora=False, r=None, alpha=None, dropout=None):
        self.model_checkpoint = model_checkpoint
        self.num_labels = num_labels
        self.device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

        if quantized_model:
            self.setup_quantized_model()
        else:
            self.setup_model()
        if lora:
            if r is None or alpha is None or dropout is None:
                raise ValueError("When 'lora' is True, 'r', 'alpha', and 'dropout' must be provided.")
            self.setup_peft_model(r=r, alpha=alpha, dropout=dropout)
        
        if not quantized_model:
            self.send_to_device()
    
    def setup_model(self):
        self.model = AutoModelForSequenceClassification.from_pretrained(self.model_checkpoint, num_labels=self.num_labels)
        if any(k in self.model_checkpoint for k in ("gpt", "opt", "bloom")):
            self.model.config.pad_token_id = self.model.config.eos_token_id

    def setup_quantized_model(self):
        self.model = AutoModelForSequenceClassification.from_pretrained(self.model_checkpoint, num_labels=self.num_labels, load_in_8bit=True)
        if any(k in self.model_checkpoint for k in ("gpt", "opt", "bloom")):
            self.model.config.pad_token_id = self.model.config.eos_token_id
        self.model = prepare_model_for_kbit_training(self.model)
    
    def setup_peft_model(self, r, alpha, dropout):
        config = LoraConfig(task_type=TaskType.SEQ_CLS, r=r, lora_alpha=alpha, lora_dropout=dropout)
        self.model = get_peft_model(self.model, config)
    
    def send_to_device(self):
        self.model.to(self.device)

class Predict:

    def __init__(self, load_dir):
        
        self.load_dir = load_dir
        self.device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

    def load_model(self):
        self.model = AutoModelForSequenceClassification.from_pretrained(self.load_dir).to(self.device)
        self.model.eval()

    def predict(self, dataloader):
        y_pred = []
        self.load_model()
        with torch.no_grad():
            for batch in dataloader:
                batch = {k: v.to(self.device) for k, v in batch.items()}
                outputs = self.model(**batch)
                logits = outputs.logits
                probs = F.softmax(logits, dim=-1)
                predictions = torch.argmax(probs, dim=-1)

                y_pred.extend(predictions.cpu().numpy())
        
        return y_pred
