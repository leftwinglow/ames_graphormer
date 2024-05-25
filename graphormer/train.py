from typing import Optional
import optuna
import torch
from tqdm import tqdm
from sklearn.metrics import accuracy_score, balanced_accuracy_score
from torch.optim.lr_scheduler import OneCycleLR, PolynomialLR, ReduceLROnPlateau
from torch_geometric.loader import DataLoader
from graphormer.cli import LossReductionType
from graphormer.config.data import DataConfig
from graphormer.config.options import SchedulerType
from graphormer.model_analysis import (
    plot_edge_path_length_bias,
    plot_node_path_length_bias,
    plot_centrality_in_degree_bias,
    plot_centrality_out_degree_bias,
    plot_layer_residual_gates,
)
from graphormer.schedulers import GreedyLR
from graphormer.config.utils import calculate_pos_weight, model_init_print, save_checkpoint
from graphormer.config.hparams import HyperparameterConfig
from optuna.trial import Trial


def train_model(
    hparam_config: HyperparameterConfig,
    trial: Optional[Trial] = None,
    train_loader: Optional[DataLoader] = None,
    test_loader: Optional[DataLoader] = None,
    data_config: Optional[DataConfig] = None,
    optimized_model: bool = False,
) -> float:
    logging_config = hparam_config.logging_config()
    if data_config is None:
        data_config = hparam_config.data_config()
    model_config = hparam_config.model_config()
    loss_config = hparam_config.loss_config()
    optimizer_config = hparam_config.optimizer_config()
    accumulation_steps = optimizer_config.accumulation_steps
    scheduler_config = hparam_config.scheduler_config()
    assert hparam_config.batch_size is not None
    assert hparam_config.last_effective_batch_num is not None

    writer = logging_config.build()
    if train_loader is None or test_loader is None:
        data_config.build()
        train_loader, test_loader = data_config.build()
    assert data_config.num_node_features is not None
    assert data_config.num_edge_features is not None
    device = torch.device(hparam_config.torch_device)
    model = (
        model_config.with_node_feature_dim(data_config.num_node_features)
        .with_edge_feature_dim(data_config.num_edge_features)
        .with_output_dim(1)
        .build()
        .to(device)
    )
    pos_weight = calculate_pos_weight(train_loader)
    loss_function = loss_config.with_pos_weight(pos_weight).build()
    optimizer = optimizer_config.build(model)
    scheduler_config = hparam_config.scheduler_config()
    if scheduler_config.scheduler_type == SchedulerType.ONE_CYCLE:
        scheduler_config = scheduler_config.with_train_batches_per_epoch(len(train_loader))
    scheduler = scheduler_config.build(optimizer)
    effective_batch_size = scheduler_config.effective_batch_size
    epochs = hparam_config.epochs
    start_epoch = hparam_config.start_epoch
    loss_reduction = optimizer_config.loss_reduction_type
    model_init_print(hparam_config, model, train_loader, test_loader)
    model.train()

    if optimized_model:
        model: torch.nn.Module = torch.compile(model, mode="max_autotune")  # type: ignore

    progress_bar = tqdm(total=0, desc="Initializing...", unit="batch")
    train_batches_per_epoch = len(train_loader)
    eval_batches_per_epoch = len(test_loader)
    avg_eval_loss = float("inf")
    for epoch in range(start_epoch, epochs):
        total_train_loss = 0.0
        total_eval_loss = 0.0

        # Set total length for training phase and update description
        progress_bar.reset(total=len(train_loader))
        progress_bar.set_description(f"Epoch {epoch+1}/{epochs} Train")

        model.train()

        avg_loss = 0.0
        train_batch_num = epoch * train_batches_per_epoch
        for batch_idx, batch in enumerate(train_loader):
            if batch_idx / train_batches_per_epoch > hparam_config.tune_size and trial is not None:
                break
            batch.to(device)
            y = batch.y.to(device)

            if train_batch_num == 0 and trial is None:
                writer.add_graph(
                    model,
                    [batch.x, batch.degrees, batch.edge_attr, batch.node_paths, batch.edge_paths],
                )
                optimizer.zero_grad()

            output = model(
                batch.x,
                batch.degrees,
                batch.edge_attr,
                batch.node_paths,
                batch.edge_paths,
            )

            loss = loss_function(output, y)

            loss.backward()

            torch.nn.utils.clip_grad_norm_(model.parameters(), hparam_config.clip_grad_norm, error_if_nonfinite=True)
            # FIX: Fix scaling of the last batch
            if should_step(batch_idx, accumulation_steps, train_batches_per_epoch):
                optimizer.step()
                optimizer.zero_grad()
                if isinstance(scheduler, OneCycleLR):
                    scheduler.step()
                    hparam_config.last_effective_batch_num += 1

            batch_loss = loss.item()
            writer.add_scalar("train/batch_loss", batch_loss, train_batch_num)
            writer.add_scalar(
                "train/sample_loss",
                batch_loss / output.shape[0] if loss_reduction == LossReductionType.SUM else batch_loss,
                train_batch_num,
            )
            total_train_loss += batch_loss

            avg_loss = total_train_loss / (progress_bar.n + 1)
            if loss_reduction == LossReductionType.SUM:
                avg_loss /= hparam_config.batch_size
            writer.add_scalar("train/avg_loss", avg_loss, train_batch_num)

            progress_bar.set_postfix_str(f"Avg Loss: {avg_loss:.4f}")
            progress_bar.update()  # Increment the progress bar
            train_batch_num += 1
        if isinstance(scheduler, PolynomialLR):
            scheduler.step()
        writer.add_scalar(
            "train/lr",
            (
                scheduler.get_last_lr()[0] * accumulation_steps
                if loss_reduction == LossReductionType.MEAN
                else scheduler.get_last_lr()[0] * effective_batch_size
            ),
            epoch,
        )

        # Prepare for the evaluation phase
        progress_bar.reset(total=len(test_loader))
        progress_bar.set_description(f"Epoch {epoch+1}/{epochs} Eval")

        all_eval_labels = []
        all_eval_preds = []

        model.eval()
        eval_batch_num = epoch * eval_batches_per_epoch
        for batch in test_loader:
            batch.to(device)
            y = batch.y.to(device)
            with torch.no_grad():
                output = model(
                    batch.x,
                    batch.degrees,
                    batch.edge_attr,
                    batch.node_paths,
                    batch.edge_paths,
                )
                loss = loss_function(output, y)
            batch_loss: float = loss.item()
            writer.add_scalar("eval/batch_loss", batch_loss, eval_batch_num)
            total_eval_loss += batch_loss

            eval_preds = torch.round(torch.sigmoid(output)).tolist()
            eval_labels = y.cpu().numpy()
            if sum(eval_labels) > 0:
                batch_bac = balanced_accuracy_score(eval_labels, eval_preds)
                writer.add_scalar("eval/batch_bac", batch_bac, eval_batch_num)

            all_eval_preds.extend(eval_preds)
            all_eval_labels.extend(eval_labels)

            progress_bar.update()  # Manually increment for each batch in eval
            eval_batch_num += 1

        if isinstance(scheduler, (ReduceLROnPlateau, GreedyLR)):
            scheduler.step(total_eval_loss)

        avg_eval_loss = total_eval_loss / len(test_loader)
        if loss_reduction == LossReductionType.SUM:
            avg_eval_loss /= float(hparam_config.batch_size)
        progress_bar.set_postfix_str(f"Avg Eval Loss: {avg_eval_loss:.4f}")
        bac = balanced_accuracy_score(all_eval_labels, all_eval_preds)
        ac = accuracy_score(all_eval_labels, all_eval_preds)
        bac_adj = balanced_accuracy_score(all_eval_labels, all_eval_preds, adjusted=True)
        writer.add_scalar("eval/acc", ac, epoch)
        writer.add_scalar("eval/bac", bac, epoch)
        writer.add_scalar("eval/bac_adj", bac_adj, epoch)
        writer.add_scalar("eval/avg_eval_loss", avg_eval_loss, epoch)
        writer.add_figure("edge_encoding_bias", plot_edge_path_length_bias(model), epoch)  # type: ignore
        writer.add_figure("node_encoding_bias", plot_node_path_length_bias(model), epoch)  # type: ignore
        writer.add_figure("centrality_in_degree_bias", plot_centrality_in_degree_bias(model), epoch)  # type: ignore
        writer.add_figure("centrality_out_degree_bias", plot_centrality_out_degree_bias(model), epoch)  # type: ignore
        writer.add_figure("residual_gates", plot_layer_residual_gates(model), epoch)  # type: ignore

        print(
            f"Epoch {epoch+1} | Avg Train Loss: {avg_loss:.4f} | Avg Eval Loss: {
                avg_eval_loss:.4f} | Eval BAC: {bac:.4f} | Eval ACC: {ac:.4f}"
        )

        if total_eval_loss < hparam_config.best_loss and trial is None:
            hparam_config.best_loss = total_eval_loss
            save_checkpoint(
                epoch,
                hparam_config,
                model,
                optimizer,
                loss_function,
                scheduler,
                "best",
            )

        if epoch % hparam_config.checkpt_save_interval == 0 and trial is None:
            save_checkpoint(
                epoch,
                hparam_config,
                model,
                optimizer,
                loss_function,
                scheduler,
            )

        if trial is not None:
            trial.report(avg_eval_loss, epoch)
            if trial.should_prune():
                raise optuna.exceptions.TrialPruned()

    progress_bar.close()
    return avg_eval_loss


def should_step(batch_idx: int, accumulation_steps: int, train_batches_per_epoch: int) -> bool:
    if accumulation_steps <= 1:
        return True
    if batch_idx > 0 and (batch_idx + 1) % accumulation_steps == 0:
        return True
    if batch_idx >= train_batches_per_epoch - 1:
        return True
    return False
