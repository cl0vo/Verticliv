"""UI integration checks without a display; no external model downloads."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from arara_factory.studio_ui import StudioWindow
from arara_factory.studio_engine import StudioProject, Crop
from arara_factory.render import MediaInfo
from arara_factory.transcribe import RecognizedWord


def test_selection_project_restore_and_word_edits():
    app=QApplication.instance() or QApplication([])
    w=StudioWindow()
    w.workspaces.setCurrentIndex(1)
    w.info=MediaInfo(640,360,120,30,True)
    w.project=StudioProject(start=15,end=45,layout='gaming',webcam_fraction=.35,zoom=True,
                           words=[RecognizedWord('hello',16,17,.9)],vocabulary='Рошан')
    w.apply_project()
    assert w.start.value()==15
    assert w.project.zoom and w.project.webcam_fraction==.35
    assert w.layout_mode.currentData()=='gaming'
    assert w.vocabulary.text()=='Рошан'
    w.table.item(0,2).setText('Привет')
    assert w.project.words[0].text=='Привет'
    w.source_canvas.frame=QImage(640,360,QImage.Format.Format_RGB32)
    w.show()
    app.processEvents()
    w.region.setCurrentIndex(1)
    area=w.source_canvas.video_rect()
    a=QPoint(int(area.x()+area.width()*.1),int(area.y()+area.height()*.1))
    b=QPoint(int(area.x()+area.width()*.4),int(area.y()+area.height()*.4))
    QTest.mousePress(w.source_canvas,Qt.MouseButton.LeftButton,pos=a)
    QTest.mouseRelease(w.source_canvas,Qt.MouseButton.LeftButton,pos=b)
    assert w.project.webcam.x==pytest.approx(.1,abs=.01)
    assert w.project.webcam.w==pytest.approx(.3,abs=.01)
    w.project.source=''
    w.close()
