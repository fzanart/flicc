import os
import copy
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.metrics import confusion_matrix, ConfusionMatrixDisplay, classification_report, accuracy_score, f1_score
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import AdamW, lr_scheduler
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

class FocalLoss(nn.Module):
    def __init__(self, alpha=None, gamma=2):
        # More the value of gamma, more importance will be given to misclassified examples
        super(FocalLoss, self).__init__()
        self.alpha = alpha
        self.gamma = gamma

    def forward(self, inputs, targets):
        ce_loss = F.cross_entropy(inputs, targets, reduction='none')
        pt = torch.exp(-ce_loss)
        focal_loss = (self.alpha[targets] * (1 - pt) ** self.gamma * ce_loss).mean()
        return focal_loss

class Engine:

    def __init__(self, epochs:int, labels:list, output_dir='output'):

        self.epochs = epochs
        self.labels = labels
        self.train_metrics = {}
        self.create_output_directory(output_dir)
        self.device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

    def create_output_directory(self, output_dir):
        if os.path.exists(output_dir):
            i = 1
            while os.path.exists(f'{output_dir}_{i}'):
                i += 1
            output_dir = f'{output_dir}_{i}'
        self.output_dir = output_dir 
        os.makedirs(output_dir)

    def plot_train_val_curves(self):
        _, ax = plt.subplots(figsize=(6, 6))
        m = pd.DataFrame(self.train_metrics).T
        ax.plot(m['Train Loss'], label='Train Loss')
        ax.plot(m['Val Loss'], label='Val Loss')
        ax.set_xlabel("Epochs")
        ax.set_ylabel("Loss")
        ax.legend()
        ax.set_title("Training and Validation Loss Curves")
        plt.savefig(f'{self.output_dir}/train_val_curves.pdf', format='pdf', bbox_inches='tight')
        plt.close()
    
    def plot_confusion_matrix(self, y_pred, y_true):
        labels = self.labels
        cm = confusion_matrix(y_true, y_pred, normalize="true")
        _, ax = plt.subplots(figsize=(6, 6))
        disp = ConfusionMatrixDisplay(confusion_matrix=cm, display_labels=labels)
        disp.plot(cmap="Blues", values_format=".2f", ax=ax, colorbar=False, xticks_rotation='vertical')
        plt.title("Normalized confusion matrix")
        plt.savefig(f'{self.output_dir}/confusion_matrix.pdf', format='pdf', bbox_inches='tight')
        plt.close()
    
    def setup_optimizer(self, lr, wd):
        self.optimizer = AdamW(self.model.parameters(), lr=lr, weight_decay=wd)
    
    def train_one_epoch(self, dataloader, focalloss, accumulation_steps):
        train_batch_loss = 0.0
        self.model.train()
        self.model.config.use_cache = False  # silence the warnings. Please re-enable for inference!
        for idx, batch in enumerate(dataloader):
            batch = {k: v.to(self.device) for k, v in batch.items()}
            outputs = self.model(**batch)
            if focalloss:
                loss = self.focal_loss(outputs.logits, batch['labels'])
            else:
                loss = outputs.loss

            # normalise the gradients
            loss = loss / accumulation_steps
            loss.backward()
            train_batch_loss += loss.item()
            
            if ((idx + 1) % accumulation_steps == 0) or (idx + 1 == len(dataloader)):
                self.optimizer.step()
                self.optimizer.zero_grad()
        
        return train_batch_loss
    
    def evaluate(self, dataloader, eval_on_best_model=False, is_quantized=False):
        y_true = []
        y_pred = []
        eval_batch_loss = 0.0
        
        if eval_on_best_model and not is_quantized:
            self.model.load_state_dict(self.best_model_state)
            self.model.eval()

        else:  
            self.model.eval()
            self.model.config.use_cache = True

        with torch.no_grad():
            for batch in dataloader:
                batch = {k: v.to(self.device) for k, v in batch.items()}
                outputs = self.model(**batch)
                logits = outputs.logits
                probs = F.softmax(logits, dim=1)
                predictions = torch.argmax(probs, dim=-1)

                eval_batch_loss += outputs.loss.item()
                y_true.extend(batch['labels'].cpu().numpy())
                y_pred.extend(predictions.cpu().numpy())

        return eval_batch_loss, y_true, y_pred
    
    def train(self, training_loader, validation_loader, test_loader, focalloss, early_stop, accumulation_steps, is_quantized):
        best_score = 0.0
        no_improvement_count = 0  # Initialize a counter for early stopping
        for epoch in range(self.epochs):
            train_batch_loss, eval_batch_loss = 0.0, 0.0
            train_batch_loss = self.train_one_epoch(training_loader, focalloss, accumulation_steps)
            eval_batch_loss, y_true, y_pred = self.evaluate(validation_loader)

            self.train_metrics[epoch + 1] = {'Train Loss':train_batch_loss / len(training_loader),
                                             'Val Loss': eval_batch_loss / len(validation_loader),
                                             'Accuracy': accuracy_score(y_true, y_pred),
                                             'F1': f1_score(y_true, y_pred, average='macro'),
                                             'True_labels': y_true,
                                             'Pred_labels': y_pred}
            
            if is_quantized:
                _, y_true, y_pred = self.evaluate(test_loader)
                self.train_metrics[epoch + 1]['Test Accuracy'] = accuracy_score(y_true, y_pred)
                self.train_metrics[epoch + 1]['Test F1'] = f1_score(y_true, y_pred, average='macro')
            
            if self.train_metrics[epoch + 1]['F1'] > best_score:
                best_score = self.train_metrics[epoch + 1]['F1']
                self.best_model_state = copy.deepcopy(self.model.state_dict())
                no_improvement_count = 0  # Reset the no improvement count
                best_epoch = epoch + 1
            else:
                no_improvement_count += 1  # Increment the no improvement count
            
            print(f"{epoch + 1} / {self.epochs}:", '\t'.join(f'{k}:\t{v:.4f}' for k, v in self.train_metrics[epoch + 1].items() if k not in ['True_labels', 'Pred_labels'])+ (" *" if epoch + 1 == best_epoch else ""))
            
            # Check for early stopping
            if no_improvement_count >= early_stop:
                print(f"No improvement for {no_improvement_count} epochs. Stopping early.")
                break

    def create_report(self, y_true, y_pred):
        with open(f'{self.output_dir}/report.txt', 'w') as report_file:
            # Write train and validation loss, validation accuracy, and F1
            report_file.write("Train Metrics:\n")
            loss_accuracy_info = f"{'Epoch':<8} {'Train Loss':<15} {'Val Loss':<15} {'Val Accuracy':<15} {'F1':<15}\n"
            for epoch, metrics in self.train_metrics.items():
                loss_accuracy_info += f"{epoch:<8} {metrics['Train Loss']:<15.4f} {metrics['Val Loss']:<15.4f} {metrics['Accuracy']:<15.4f} {metrics['F1']:<15.4f}\n"
            report_file.write(loss_accuracy_info)
            classification_report_text = classification_report(y_true=y_true, y_pred=y_pred, target_names=self.labels, zero_division=0.0)
            report_file.write(f"Classification Report:\n{classification_report_text}\n\n")
        self.plot_confusion_matrix(y_true, y_pred)
        self.plot_train_val_curves()

    def calculate_class_weights(self):
        y_train = self.dataset_encoded['train']['labels'].cpu().numpy()
        class_counts = np.bincount(y_train)
        total_samples = len(y_train)

        self.class_weights = []
        for count in class_counts:
            weight = 1 / (count / total_samples)
            self.class_weights.append(weight)

    def setup_focal_loss(self, gamma):
        self.calculate_class_weights()
        alpha = torch.tensor(self.class_weights).to(self.device)
        self.focal_loss = FocalLoss(alpha=alpha, gamma=gamma)

    def run(self, **kwargs):

        focalloss = False
        early_stop = self.epochs
        accumulation_steps = 1
        is_quantized = False

        if 'focalloss' in kwargs:
            self.calculate_class_weights()
            self.setup_focal_loss(kwargs['gamma'])
            focalloss = True
        
        if 'early_stop' in kwargs:
            early_stop = kwargs['early_stop']

        if 'is_quantized' in kwargs:
            is_quantized = kwargs['is_quantized']

        if 'accumulation_steps' in kwargs:
            accumulation_steps = kwargs['accumulation_steps']

        self.setup_optimizer(lr=kwargs['lr'], wd=kwargs['wd'])

        self.train(training_loader=kwargs['train_dataloader'],
                    validation_loader=kwargs['eval_dataloader'],
                    test_loader=kwargs['test_dataloader'],
                    focalloss=focalloss,
                    early_stop=early_stop,
                    accumulation_steps=accumulation_steps,
                    is_quantized=is_quantized)
        
        if not is_quantized:
            _, eval_y_true, eval_y_pred = self.evaluate(dataloader=kwargs['eval_dataloader'], eval_on_best_model=True, is_quantized=is_quantized)
            print('best (higgest macro f1-score) val results:')
            print(classification_report(y_true=eval_y_true, y_pred=eval_y_pred, target_names=self.labels, zero_division=0.0))
            _, test_y_true, test_y_pred = self.evaluate(dataloader=kwargs['test_dataloader'], eval_on_best_model=True, is_quantized=is_quantized)
            print('test results:')
            print(classification_report(y_true=test_y_true, y_pred=test_y_pred, target_names=self.labels, zero_division=0.0))
            self.create_report(test_y_true, test_y_pred)
            return accuracy_score(test_y_true, test_y_pred), f1_score(test_y_true, test_y_pred, average='macro'), accuracy_score(eval_y_true, eval_y_pred), f1_score(eval_y_true, eval_y_pred, average='macro')
    
    @classmethod
    def plot_grid_search(cls, df:dict, title:str, column:str, sci_format:bool, log_xscale=False):
        # Create the plot
        df = pd.DataFrame(df)
        ax = df.plot(x=column, y=['test_acc', 'test_f1'], marker='o', linestyle='-')
        # Set y-axis range between 0 and 1
        plt.ylim(0, 1)
        # Annotate points with F1 scores
        for _, row in df.iterrows(): 
            ax.annotate(f'{row["test_f1"]:.2f}', (row[column], row["test_f1"]), textcoords='offset points', xytext=(0, -10), ha='center')
        # Annotate points with Acc scores
        for _, row in df.iterrows():
            ax.annotate(f'{row["test_acc"]:.2f}', (row[column], row["test_acc"]), textcoords='offset points', xytext=(0, 10), ha='center')

        # x-format
        if sci_format:
            plt.xticks(df[column], [f'{val:.0e}' for val in df[column]], ha='center')
        else:
            plt.xticks(df[column], [val for val in df[column]], ha='center')
        if log_xscale:
            plt.xscale('log')

        plt.minorticks_off()
        plt.title(title)
        plt.xlabel(column)
        plt.ylabel('Score')
        plt.show()

    def save_best_model(self):
        model_path = os.path.join(self.output_dir, 'model')
        self.model.load_state_dict(self.best_model_state)
        self.model.save_pretrained(model_path)
        