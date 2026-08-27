# Contributing — ticketoff

Правила совместной работы с git для команды **ticketoff**. Одинаковы для всех трёх репозиториев (`backend`, `web`, `mobile`).

## 1. Модель ветвления
- Единственная постоянная ветка — **`main`**. Она всегда в рабочем состоянии (готова к демо).
- **Прямые коммиты в `main` запрещены** — только через Pull Request (1 апрув, squash-merge).
- На каждую задачу Jira — своя ветка, отколотая от свежего `main`.

## 2. Создание ветки
Всегда сначала обновляем `main`:
```bash
git checkout main
git pull
git checkout -b issue/TICK-123
```
- Префикс всегда `issue/`, дальше — код задачи из Jira.
- Можно добавить короткий слаг для читаемости: `issue/TICK-123-login-form`.

## 3. Формат коммитов
```
type(scope): short description in english (TICK-123)
```
- **type**: feat · fix · refactor · ci · docs · chore · test · perf · build
- **scope**: файл или область задачи (auth, tickets, seatmap, …)
- **description**: кратко, на английском, в повелительном наклонении, без точки в конце
- **(TICK-123)**: код задачи Jira — связывает коммит с задачей

Примеры:
```
feat(auth): add email verification endpoint (TICK-14)
fix(tickets): prevent double seat hold on concurrent checkout (TICK-52)
refactor(orders): extract price calculation into service (TICK-33)
ci(backend): add ruff and mypy to pipeline (TICK-7)
```

## 4. Pull Request
- Мы используем **squash-merge** — итоговый коммит в `main` равен **заголовку PR**.
- Поэтому **название PR пиши в том же формате коммита**: `type(scope): ... (TICK-123)`.
- Заполни PR-шаблон, укажи задачу Jira.
- Для merge нужно: **1 апрув**, все треды resolved, зелёный CI.
- Ветка удаляется автоматически после merge.

## 5. Обновление ветки (если main ушёл вперёд)
```bash
git checkout main && git pull
git checkout issue/TICK-123
git rebase main
# решаем конфликты, затем:
git push --force-with-lease
```
Мы держим линейную историю — используем **rebase**, а не merge.

## 6. Definition of Done
Задача закрыта, только когда код: **интегрирован, отревьюен, покрыт тестами** (где применимо) **и задокументирован**.
