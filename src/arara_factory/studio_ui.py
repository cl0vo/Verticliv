from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QRectF, QUrl
from PySide6.QtGui import QColor, QPainter, QPen, QImage, QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox, QLineEdit,
    QFormLayout, QSplitter, QSlider, QTabWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QProgressBar, QMessageBox, QListWidget,
    QScrollArea, QAbstractItemView,
)

from .studio_engine import (
    StudioProject, Crop, fitted_crop, transcribe,
    export_video, find_highlights, source_info,
)
from .transcribe import RecognizedWord


class Task(QThread):
    progress = Signal(int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.done.emit(self.operation(self.progress.emit, self.isInterruptionRequested))
        except Exception as exc:
            self.failed.emit(str(exc))


class SourceCanvas(QWidget):
    selected = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame = QImage()
        self.project = StudioProject()
        self.mode = 'main'
        self.anchor = None
        self.draft = None
        self.setMinimumSize(400, 225)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def video_rect(self):
        if self.frame.isNull():
            return QRectF()
        scale = min(self.width() / self.frame.width(), self.height() / self.frame.height())
        w, h = self.frame.width() * scale, self.frame.height() * scale
        return QRectF((self.width()-w)/2, (self.height()-h)/2, w, h)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor('#090e17'))
        area = self.video_rect()
        if self.frame.isNull():
            p.setPen(QColor('#9daac0'))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, 'Загрузи видео — затем выдели область мышью')
            return
        p.drawImage(area, self.frame)
        for name, color in [('main', '#b0f563'), ('webcam', '#9b8cff')]:
            if name == 'webcam' and self.project.layout != 'gaming':
                continue
            crop = getattr(self.project, name)
            rect = QRectF(area.x()+crop.x*area.width(), area.y()+crop.y*area.height(), crop.w*area.width(), crop.h*area.height())
            p.setPen(QPen(QColor(color), 2))
            p.drawRect(rect)
            p.drawText(rect.adjusted(6, 6, -6, -6), Qt.AlignmentFlag.AlignTop, 'ВЕБКА' if name == 'webcam' else 'ОСНОВНОЙ КАДР / ИГРА')
        if self.draft:
            p.setPen(QPen(QColor('#ffffff'), 2, Qt.PenStyle.DashLine))
            p.drawRect(self.draft)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.video_rect().contains(event.position()):
            self.anchor = event.position()

    def mouseMoveEvent(self, event):
        if self.anchor is not None:
            self.draft = QRectF(self.anchor, event.position()).normalized().intersected(self.video_rect())
            self.update()

    def mouseReleaseEvent(self, event):
        if self.anchor is not None:
            area = self.video_rect()
            rect = QRectF(self.anchor, event.position()).normalized().intersected(area)
            if rect.width() >= 12 and rect.height() >= 12:
                crop = Crop((rect.x()-area.x())/area.width(), (rect.y()-area.y())/area.height(), rect.width()/area.width(), rect.height()/area.height())
                self.selected.emit(self.mode, crop)
        self.anchor = self.draft = None
        self.update()


class OutputCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame = QImage()
        self.project = StudioProject()
        self.position = 0
        self.setMinimumSize(200, 356)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor('#090e17'))
        scale = min(self.width()/1080, self.height()/1920)
        p.translate((self.width()-1080*scale)/2, (self.height()-1920*scale)/2)
        p.scale(scale, scale)
        if self.frame.isNull():
            return
        project = self.project
        width, height = self.frame.width(), self.frame.height()
        if project.zoom and project.zoom_at <= self.position <= project.zoom_at+project.zoom_duration:
            p.translate(540, 960)
            p.scale(1.12, 1.12)
            p.translate(-540, -960)
        def draw(crop, dest):
            x, y, w, h = fitted_crop(crop, width, height, dest.width()/dest.height())
            p.drawImage(dest, self.frame, QRectF(x, y, w, h))
        if project.layout == 'gaming':
            cam_h = int(1920*project.webcam_fraction)//2*2
            draw(project.webcam, QRectF(0, 0, 1080, cam_h))
            draw(project.main, QRectF(0, cam_h, 1080, 1920-cam_h))
        elif project.layout == 'fill':
            draw(project.main, QRectF(0, 0, 1080, 1920))
        else:
            x, y, w, h = project.main.pixels(width, height)
            # Fast proxy of the export's blurred background.
            tiny = self.frame.copy(x,y,w,h).scaled(24,42,Qt.AspectRatioMode.IgnoreAspectRatio,Qt.TransformationMode.SmoothTransformation)
            p.drawImage(QRectF(0,0,1080,1920), tiny.scaled(1080,1920,Qt.AspectRatioMode.IgnoreAspectRatio,Qt.TransformationMode.SmoothTransformation))
            factor = min(1080/w,1920/h)
            p.drawImage(QRectF((1080-w*factor)/2,(1920-h*factor)/2,w*factor,h*factor), self.frame, QRectF(x,y,w,h))
        p.resetTransform()
        p.translate((self.width()-1080*scale)/2, (self.height()-1920*scale)/2)
        p.scale(scale, scale)
        if project.captions:
            from .subtitles import group_words
            for group in group_words(project.words, max_words=4, max_chars=32):
                if group.start <= self.position <= group.end:
                    font = p.font()
                    font.setPixelSize(project.font_size)
                    font.setBold(True)
                    p.setFont(font)
                    area = QRectF(70,project.caption_y-160,940,160)
                    p.fillRect(area,QColor(0,0,0,145))
                    p.setPen(QColor('white'))
                    p.drawText(area,Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom | Qt.TextFlag.TextWordWrap,' '.join(w.text for w in group.words))
                    break


class StudioWindow(QMainWindow):
    def __init__(self, legacy_factory=None):
        super().__init__()
        self.project = StudioProject()
        self.info = None
        self.task = None
        self.legacy_factory = legacy_factory
        self.legacy = None
        self.last_output = None
        self.setWindowTitle('Vertical Studio — Reels · Shorts · стримы')
        self.resize(1360, 900)
        self.setAcceptDrops(True)
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        header = QHBoxLayout()
        title = QLabel('VERTICAL / STUDIO')
        title.setStyleSheet('font-size:24px; font-weight:800; color:#b0f563')
        header.addWidget(title)
        header.addStretch()
        self.import_button = self.button('＋ Видео', self.choose_video, header)
        self.open_project_button = self.button('Открыть проект', self.load_project, header)
        self.save_project_button = self.button('Сохранить проект', self.save_project, header)
        self.legacy_button = self.button('Ещё: Brainrot / публикация', self.open_legacy, header)
        outer.addLayout(header)
        self.filename = QLabel('Перетащи видео в окно или нажми «＋ Видео»')
        outer.addWidget(self.filename)
        self.editor = QSplitter()
        outer.addWidget(self.editor,1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.source_canvas = SourceCanvas()
        self.source_canvas.selected.connect(self.set_crop)
        left_layout.addWidget(QLabel('ИСХОДНИК · выделение области перетягиванием мыши'))
        left_layout.addWidget(self.source_canvas,1)
        playback = QHBoxLayout()
        self.button('▶ / ❚❚',self.toggle_play,playback)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0,0)
        self.seek.sliderMoved.connect(lambda v:self.player.setPosition(v))
        playback.addWidget(self.seek,1)
        self.clock = QLabel('00:00')
        playback.addWidget(self.clock)
        left_layout.addLayout(playback)
        selection = QHBoxLayout()
        self.region = QComboBox()
        self.region.addItem('Выделять основной кадр / игру','main')
        self.region.addItem('Выделять веб-камеру','webcam')
        self.region.currentIndexChanged.connect(lambda:self.change_region())
        selection.addWidget(self.region)
        self.button('Сбросить область',self.reset_crop,selection)
        left_layout.addLayout(selection)
        time_row = QHBoxLayout()
        self.start = QDoubleSpinBox()
        self.end = QDoubleSpinBox()
        for label, spin in [('Начало, сек', self.start),('Конец, сек',self.end)]:
            spin.setRange(0,864000)
            spin.setDecimals(2)
            time_row.addWidget(QLabel(label))
            time_row.addWidget(spin)
        self.end.setValue(60)
        self.button('Начало здесь',lambda:self.start.setValue(self.player.position()/1000),time_row)
        self.button('Конец здесь',lambda:self.end.setValue(self.player.position()/1000),time_row)
        self.button('Всё видео',self.full_range,time_row)
        left_layout.addLayout(time_row)
        self.tabs = QTabWidget()
        left_layout.addWidget(self.tabs,1)
        captions = QWidget()
        caps = QVBoxLayout(captions)
        tools_row = QHBoxLayout()
        self.recognize_button = self.button('Распознать выбранный фрагмент',self.recognize,tools_row)
        self.button('＋ Строка', self.add_word,tools_row)
        self.button('Удалить выбранные',self.delete_words,tools_row)
        manual = QPushButton('Использовать таблицу без повторного распознавания')
        manual.clicked.connect(self.accept_manual_captions)
        caps.addWidget(manual)
        caps.addLayout(tools_row)
        caps.addWidget(QLabel('Тайминги в секундах исходного видео. Двойной клик — исправить текст.'))
        self.table = QTableWidget(0,3)
        self.table.setHorizontalHeaderLabels(['Начало','Конец','Слово / фраза'])
        self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.cellChanged.connect(self.update_words)
        self.table.cellDoubleClicked.connect(self.seek_caption)
        caps.addWidget(self.table)
        self.tabs.addTab(captions,'Субтитры')
        highlights = QWidget()
        hl = QVBoxLayout(highlights)
        hl.addWidget(QLabel('Кандидаты по пикам звука. Это поиск реакций, а не распознавание убийств в игре.'))
        row = QHBoxLayout()
        self.highlight_length = QSpinBox()
        self.highlight_length.setRange(10,180)
        self.highlight_length.setValue(30)
        row.addWidget(QLabel('Длина клипа, сек'))
        row.addWidget(self.highlight_length)
        self.button('Найти моменты во всём видео',self.highlights,row)
        hl.addLayout(row)
        self.highlights_list = QListWidget()
        self.highlights_list.itemClicked.connect(self.select_highlight)
        hl.addWidget(self.highlights_list)
        self.tabs.addTab(highlights,'Хайлайты')
        self.editor.addWidget(left)
        middle = QWidget()
        mid = QVBoxLayout(middle)
        mid.addWidget(QLabel('РЕЗУЛЬТАТ 9:16'))
        self.output_canvas = OutputCanvas()
        mid.addWidget(self.output_canvas,1)
        note = QLabel('Быстрый предпросмотр. Точный вид анимации субтитров и размытия — кнопка «Тест 5 сек».')
        note.setWordWrap(True)
        mid.addWidget(note)
        self.editor.addWidget(middle)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        controls = QWidget()
        form = QFormLayout(controls)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.layout_mode = QComboBox()
        for label, value in [('Весь кадр + размытие','fit'),('Заполнить 9:16','fill'),('Игра + вебка сверху','gaming')]:
            self.layout_mode.addItem(label,value)
        form.addRow('Компоновка',self.layout_mode)
        self.cam_size = QSpinBox()
        self.cam_size.setRange(15,50)
        self.cam_size.setValue(28)
        self.cam_size.setSuffix('%')
        form.addRow('Высота вебки',self.cam_size)
        self.captions_enabled = QCheckBox('Добавить субтитры')
        self.captions_enabled.setChecked(True)
        form.addRow(self.captions_enabled)
        self.model = QComboBox()
        for label,value in [('Small · быстрее','small'),('Medium · точнее','medium'),('Large v3 · максимум','large-v3'),('Turbo · баланс','turbo')]:
            self.model.addItem(label,value)
        form.addRow('Распознавание',self.model)
        self.language = QComboBox()
        for label,value in [('Русский','ru'),('Авто','auto'),('English','en')]:
            self.language.addItem(label,value)
        form.addRow('Язык',self.language)
        self.device = QComboBox()
        self.device.addItem('CPU · без настройки','cpu')
        self.device.addItem('NVIDIA · CUDA 12 / cuDNN 9','cuda')
        form.addRow('Вычисления',self.device)
        hint = QLabel('Первый запуск скачает модель. Large требует больше памяти и времени. Видео не отправляется на сервер распознавания.')
        hint.setWordWrap(True)
        form.addRow(hint)
        self.vocabulary = QLineEdit()
        self.vocabulary.setPlaceholderText('Имена, термины, названия героев…')
        form.addRow('Словарь',self.vocabulary)
        dota = QPushButton('Добавить словарь Dota 2')
        dota.clicked.connect(lambda:self.vocabulary.setText('Дота 2, Рошан, Аегис, БКБ, ульт, байбек, керри, саппорт, мид, инвокер, пудж, рампага, Black King Bar.'))
        form.addRow(dota)
        self.style = QComboBox()
        self.style.addItem('Подсветка слов','karaoke')
        self.style.addItem('Обычные фразы','plain')
        form.addRow('Стиль',self.style)
        self.font_size = QSpinBox()
        self.font_size.setRange(24,100)
        self.font_size.setValue(64)
        form.addRow('Размер текста',self.font_size)
        self.caption_y = QSpinBox()
        self.caption_y.setRange(200,1750)
        self.caption_y.setValue(1500)
        form.addRow('Позиция текста Y',self.caption_y)
        self.zoom = QCheckBox('Акцентный зум ×1.12')
        form.addRow(self.zoom)
        self.zoom_at = QDoubleSpinBox()
        self.zoom_at.setRange(0,864000)
        self.zoom_at.setDecimals(2)
        form.addRow('Момент зума, сек',self.zoom_at)
        zoom_here = QPushButton('Зум в текущем кадре')
        zoom_here.clicked.connect(lambda:self.zoom_at.setValue(self.player.position()/1000))
        form.addRow(zoom_here)
        scroll.setWidget(controls)
        scroll.setMinimumWidth(285)
        self.editor.addWidget(scroll)
        self.editor.setSizes([740,280,310])
        footer = QHBoxLayout()
        self.status = QLabel('Готов к работе')
        self.status.setWordWrap(True)
        self.status.setMaximumWidth(550)
        footer.addWidget(self.status,1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(160)
        footer.addWidget(self.progress)
        self.cancel_button = self.button('Отмена',self.cancel,footer)
        self.cancel_button.setEnabled(False)
        self.preview_button = self.button('Тест 5 сек',lambda:self.export(True),footer)
        self.export_button = self.button('Создать вертикалку',lambda:self.export(False),footer)
        self.export_button.setStyleSheet('background:#b0f563;color:#111827;font-weight:800;padding:12px')
        self.open_output_button = self.button('Открыть результат',self.open_output,footer)
        outer.addLayout(footer)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self.on_frame)
        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(lambda d:self.seek.setRange(0,d))
        self.player.errorOccurred.connect(lambda *args:self.status.setText('Предпросмотр: '+self.player.errorString()))
        for combo in (self.layout_mode,self.model,self.language,self.device,self.style):
            combo.currentIndexChanged.connect(self.sync)
        for spin in (self.start,self.end,self.cam_size,self.font_size,self.caption_y,self.zoom_at):
            spin.valueChanged.connect(self.sync)
        for box in (self.captions_enabled,self.zoom):
            box.toggled.connect(self.sync)
        self.vocabulary.textChanged.connect(self.sync)
        self.sync()
        self.setStyleSheet('''QWidget { background:#111827; color:#e6edf7; font-family:"Segoe UI"; font-size:12px; }
QPushButton { background:#263348; border:1px solid #3b4a61; border-radius:6px; padding:8px; }
QPushButton:hover { background:#35445d; } QPushButton:disabled {color:#62718a;}
QLineEdit,QSpinBox,QDoubleSpinBox,QComboBox {padding:6px;background:#192437;border:1px solid #344259;border-radius:4px;}
QTableWidget,QListWidget {background:#101a2b;gridline-color:#2e3d52;} QHeaderView::section {background:#243248;padding:6px;}
QTabBar::tab {padding:9px 20px;background:#1b283d;} QTabBar::tab:selected {color:#b0f563;background:#2a3a50;}
QProgressBar {border:1px solid #344259;border-radius:4px;text-align:center;} QProgressBar::chunk {background:#689d39;}
''')

    @staticmethod
    def button(text, callback, layout):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def sync(self, *args):
        p = self.project
        p.start,p.end = self.start.value(),self.end.value()
        p.layout = self.layout_mode.currentData()
        p.webcam_fraction = self.cam_size.value()/100
        p.captions,p.zoom = self.captions_enabled.isChecked(),self.zoom.isChecked()
        p.model,p.language,p.device = self.model.currentData(),self.language.currentData(),self.device.currentData()
        p.caption_style,p.font_size,p.caption_y = self.style.currentData(),self.font_size.value(),self.caption_y.value()
        p.vocabulary,p.zoom_at = self.vocabulary.text(),self.zoom_at.value()
        self.source_canvas.project = self.output_canvas.project = p
        self.source_canvas.update()
        self.output_canvas.update()

    def apply_project(self):
        p = copy.deepcopy(self.project)
        for control,value in [(self.start,p.start),(self.end,p.end),(self.cam_size,round(p.webcam_fraction*100)),(self.font_size,p.font_size),(self.caption_y,p.caption_y),(self.zoom_at,p.zoom_at)]:
            control.blockSignals(True)
            control.setValue(value)
            control.blockSignals(False)
        for control,value in [(self.layout_mode,p.layout),(self.model,p.model),(self.language,p.language),(self.device,p.device),(self.style,p.caption_style)]:
            control.blockSignals(True)
            control.setCurrentIndex(max(0,control.findData(value)))
            control.blockSignals(False)
        self.captions_enabled.blockSignals(True)
        self.captions_enabled.setChecked(p.captions)
        self.captions_enabled.blockSignals(False)
        self.zoom.blockSignals(True)
        self.zoom.setChecked(p.zoom)
        self.zoom.blockSignals(False)
        self.vocabulary.blockSignals(True)
        self.vocabulary.setText(p.vocabulary)
        self.vocabulary.blockSignals(False)
        self.fill_table()
        self.sync()

    def choose_video(self):
        name,_ = QFileDialog.getOpenFileName(self,'Выбрать видео','','Видео (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts);;Все файлы (*)')
        if name:
            self.open_video(name)

    def open_video(self,name,project=None):
        if self.task and self.task.isRunning():
            return
        try:
            info = source_info(name)
            candidate = project or StudioProject(source=str(Path(name).resolve()),end=min(60,info.duration),captions=info.has_audio)
            candidate.validate(info.duration)
            self.project,self.info = candidate,info
            self.filename.setText(f'{Path(name).name}  ·  {info.width}×{info.height}  ·  {info.duration:.1f} сек')
            self.player.setSource(QUrl.fromLocalFile(str(Path(name).resolve())))
            self.player.play()
            self.highlights_list.clear()
            self.apply_project()
            self.status.setText('Выбери фрагмент и компоновку. Кнопка экспорта сама распознает речь.')
        except Exception as exc:
            QMessageBox.warning(self,'Не удалось открыть видео',str(exc))

    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() and not (self.task and self.task.isRunning()):
            event.acceptProposedAction()

    def dropEvent(self,event):
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            self.open_video(urls[0].toLocalFile())

    def on_frame(self,frame):
        image = frame.toImage()
        if not image.isNull():
            self.source_canvas.frame = self.output_canvas.frame = image
            self.source_canvas.update()
            self.output_canvas.update()

    def on_position(self,position):
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        seconds = position//1000
        self.clock.setText(f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}')
        self.output_canvas.position = position/1000
        self.output_canvas.update()

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def change_region(self):
        self.source_canvas.mode = self.region.currentData()
        if self.source_canvas.mode == 'webcam':
            self.layout_mode.setCurrentIndex(self.layout_mode.findData('gaming'))

    def set_crop(self,name,crop):
        setattr(self.project,name,crop)
        self.sync()

    def reset_crop(self):
        self.set_crop(self.region.currentData(),Crop())

    def full_range(self):
        if self.info:
            self.start.setValue(0)
            self.end.setValue(self.info.duration)

    def checked_project(self):
        if not self.info:
            raise ValueError('Сначала загрузи видео')
        self.sync()
        if not self.update_words():
            raise ValueError('Исправь тайминги в таблице субтитров')
        self.project.validate(self.info.duration)
        return copy.deepcopy(self.project)

    def launch(self,operation,done):
        if self.task and self.task.isRunning():
            return
        self.player.pause()
        self.task = Task(operation,self)
        self.task.progress.connect(self.on_progress)
        self.task.done.connect(done)
        self.task.failed.connect(self.failed)
        self.task.finished.connect(self.idle)
        self.editor.setEnabled(False)
        for control in (self.import_button,self.open_project_button,self.save_project_button,self.legacy_button,self.export_button,self.preview_button):
            control.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self.status.setText('Подготовка…')
        self.task.start()

    def on_progress(self,value,text):
        self.progress.setValue(value)
        self.status.setText(text)

    def idle(self):
        self.editor.setEnabled(True)
        for control in (self.import_button,self.open_project_button,self.save_project_button,self.legacy_button,self.export_button,self.preview_button):
            control.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def failed(self,error):
        self.status.setText(error[:180])
        if error != 'Операция отменена':
            QMessageBox.warning(self,'Не удалось завершить',error)

    def cancel(self):
        if self.task:
            self.task.requestInterruption()
            self.status.setText('Отмена запрошена. При загрузке модели дождись её завершения.')

    def accept_manual_captions(self):
        try:
            p = self.checked_project()
            self.project.transcript_ranges.append([p.start, p.end])
            self.status.setText('Для выбранного фрагмента будут использованы слова из таблицы.')
        except Exception as exc:
            self.failed(str(exc))

    def recognize(self):
        try:
            p = self.checked_project()
            self.launch(lambda progress,cancel:(p,transcribe(p,progress,cancel)),self.transcribed)
        except Exception as exc:
            self.failed(str(exc))

    def transcribed(self,result):
        p,words = result
        self.project.words = sorted([w for w in self.project.words if w.end <= p.start or w.start >= p.end]+words,key=lambda w:w.start)
        self.project.transcript_ranges.append([p.start,p.end])
        self.fill_table()
        self.status.setText(f'Распознано слов: {len(words)}. Проверь имена и игровые термины.')

    def fill_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.project.words))
        for row,word in enumerate(self.project.words):
            for col,value in enumerate((f'{word.start:.3f}',f'{word.end:.3f}',word.text)):
                item = QTableWidgetItem(value)
                if word.confidence < .6:
                    item.setBackground(QColor('#5a4027'))
                    item.setToolTip('Низкая уверенность распознавания — проверь слово')
                self.table.setItem(row,col,item)
        self.table.blockSignals(False)
        self.output_canvas.update()

    def update_words(self,*args):
        words = []
        try:
            for row in range(self.table.rowCount()):
                values = [self.table.item(row,col).text() for col in range(3)]
                start,end = float(values[0]),float(values[1])
                if not 0 <= start < end or (self.info and end > self.info.duration+.05):
                    raise ValueError()
                if values[2].strip():
                    words.append(RecognizedWord(values[2].strip(),start,end,1))
            self.project.words = sorted(words,key=lambda w:w.start)
            self.output_canvas.update()
            return True
        except (ValueError,AttributeError):
            self.status.setText('Ошибка тайминга: начало должно быть меньше конца и внутри видео')
            return False

    def add_word(self):
        if self.info:
            t = min(self.player.position()/1000,max(0,self.info.duration-.5))
            self.project.words.append(RecognizedWord('Текст',t,min(t+.5,self.info.duration),1))
            self.project.words.sort(key=lambda w:w.start)
            self.fill_table()

    def delete_words(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()},reverse=True)
        self.table.blockSignals(True)
        for row in rows:
            self.table.removeRow(row)
        self.table.blockSignals(False)
        self.update_words()

    def seek_caption(self,row,col):
        try:
            self.player.setPosition(int(float(self.table.item(row,0).text())*1000))
        except ValueError:
            pass

    def highlights(self):
        try:
            p = self.checked_project()
            length = self.highlight_length.value()
            self.launch(lambda progress,cancel:find_highlights(p.source,length,progress,cancel),self.show_highlights)
        except Exception as exc:
            self.failed(str(exc))

    def show_highlights(self,items):
        self.highlights_list.clear()
        for h in items:
            self.highlights_list.addItem(f'{h.start:.1f}–{h.end:.1f} сек · {h.reason}')
            self.highlights_list.item(self.highlights_list.count()-1).setData(Qt.ItemDataRole.UserRole,h)
        self.status.setText(f'Найдено кандидатов: {len(items)}. Нажми на момент для просмотра.')

    def select_highlight(self,item):
        h = item.data(Qt.ItemDataRole.UserRole)
        self.start.setValue(h.start)
        self.end.setValue(h.end)
        self.zoom_at.setValue(h.peak)
        self.player.setPosition(int(h.start*1000))
        self.player.play()

    def export(self,preview=False):
        try:
            p = self.checked_project()
            if preview:
                p.end = min(p.end,p.start+5)
            suggested = str(Path(p.source).with_name(Path(p.source).stem+('-test' if preview else '-vertical')+'.mp4'))
            name,_ = QFileDialog.getSaveFileName(self,'Сохранить вертикалку',suggested,'Видео (*.mp4)')
            if not name:
                return
            target = Path(name).with_suffix('.mp4')
            if target.resolve() == Path(p.source).resolve():
                raise ValueError('Выбери другое имя: исходник нельзя перезаписать')
            def operation(progress,cancel):
                fresh = None
                if p.captions and not any(a <= p.start+.01 and b >= p.end-.01 for a,b in p.transcript_ranges):
                    fresh = transcribe(p,progress,cancel)
                    # Extending the selection must preserve already corrected words.
                    fresh = [w for w in fresh if not any(w.start < old.end and w.end > old.start for old in p.words)]
                    p.words = sorted(p.words + fresh, key=lambda w: w.start)
                path = export_video(p,target,progress,cancel)
                return path,p,fresh
            self.launch(operation,self.exported)
        except Exception as exc:
            self.failed(str(exc))

    def exported(self,result):
        path,p,fresh = result
        if fresh is not None:
            self.project.words = p.words
            self.project.transcript_ranges.append([p.start,p.end])
            self.fill_table()
        self.last_output = path
        self.progress.setValue(100)
        self.status.setText('Сохранено: '+path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_output(self):
        if self.last_output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_output))

    def save_project(self):
        try:
            p = self.checked_project()
            name,_ = QFileDialog.getSaveFileName(self,'Сохранить проект','','Проект Vertical (*.vertical.json)')
            if name:
                p.save(Path(name))
                self.status.setText('Проект сохранён. Исходное видео хранится отдельно.')
        except Exception as exc:
            self.failed(str(exc))

    def load_project(self):
        name,_ = QFileDialog.getOpenFileName(self,'Открыть проект','','Проект Vertical (*.json)')
        if name:
            try:
                p = StudioProject.load(Path(name))
                if not Path(p.source).is_file():
                    source,_ = QFileDialog.getOpenFileName(self,'Найди исходное видео проекта')
                    if not source:
                        return
                    p.source = source
                self.open_video(p.source,p)
            except Exception as exc:
                self.failed(str(exc))

    def open_legacy(self):
        if self.legacy_factory:
            if self.legacy is None:
                self.legacy = self.legacy_factory()
            self.legacy.show()
            self.legacy.raise_()

    def closeEvent(self,event):
        if self.task and self.task.isRunning():
            self.cancel()
            self.status.setText('Дождись отмены обработки перед закрытием окна.')
            event.ignore()
            return
        if self.project.source:
            answer = QMessageBox.question(self,'Закрыть редактор?','Несохранённые изменения проекта будут потеряны. Закрыть?',QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.player.stop()
        event.accept()
